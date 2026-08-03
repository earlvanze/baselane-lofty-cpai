import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from build_monthly_discord_all_send_plan import (  # noqa: E402
    bound_message_bytes,
    build_plan,
    compact_message,
)
from discord_summary_routing_policy import (  # noqa: E402
    DRAFT_REVIEW_PREFIX,
    EARLCOIN_GUILD_ID,
    EARLCOIN_REVIEW_FORUM_ID,
    EARLCOIN_REVIEW_TARGET,
    LOFTY_GUILD_ID,
)


def test_compaction_does_not_inject_a_stale_accuracy_notice() -> None:
    source = "Property Update: Example\n\n## 2026-07-31\n"
    message, compacted = compact_message(source)

    assert compacted is False
    assert message == source


def test_financial_detail_compaction_does_not_duplicate_prefix_text() -> None:
    source = (
        "Property Update: Example\n\n"
        + ("long operational detail " * 150)
        + "\nFinancial detail:\n\n## Cash Flow Snapshot (2026-07)\n\nRevenue: $1.00\n"
    )

    bounded, compacted = bound_message_bytes(source)

    assert compacted is True
    assert bounded.count("Property Update: Example") == 1
    assert bounded.count("Financial detail:") == 1
    assert len(bounded.encode("utf-8")) <= 2000


def test_all_property_plan_routes_only_to_earlcoin_review_forum(tmp_path: Path) -> None:
    update = tmp_path / "Updates.md"
    update.write_text("Monthly facts.\n", encoding="utf-8")

    plan = build_plan(
        {
            "run_month": "2026-07",
            "reporting_cutoff_date": "2026-07-31",
            "records": [
                {
                    "property_name": "Test House Public",
                    "update_candidate": str(update),
                }
            ],
        }
    )

    assert plan["guild_id"] == EARLCOIN_GUILD_ID
    assert plan["reporting_cutoff_date"] == "2026-07-31"
    assert plan["forum_id"] == EARLCOIN_REVIEW_FORUM_ID
    assert plan["target"] == EARLCOIN_REVIEW_TARGET
    assert plan["guild_id"] != LOFTY_GUILD_ID
    record = plan["records"][0]
    assert record["guild_id"] == EARLCOIN_GUILD_ID
    assert record["target"] == EARLCOIN_REVIEW_TARGET
    assert record["thread_name"] == "Test House"
    assert record["route_matched"] is True
    assert len((DRAFT_REVIEW_PREFIX + record["message"]).encode("utf-8")) <= 2000
    assert plan["active_portfolio_summary_population_ok"] is False
    assert "active_property_count_missing_or_invalid" in plan["issues"]


def test_all_property_plan_fails_closed_when_cutoff_is_not_month_end(tmp_path: Path) -> None:
    update = tmp_path / "Updates.md"
    update.write_text("Monthly facts.\n", encoding="utf-8")

    plan = build_plan(
        {
            "run_month": "2026-07",
            "reporting_cutoff_date": "2026-07-14",
            "records": [{"property_name": "Test House", "update_candidate": str(update)}],
        }
    )

    assert plan["status"] == "review"
    assert plan["expected_reporting_cutoff_date"] == "2026-07-31"
    assert any("reporting_cutoff_not_month_end" in issue for issue in plan["issues"])


def test_all_property_plan_requires_one_summary_per_reporting_target(tmp_path: Path) -> None:
    update = tmp_path / "Updates.md"
    update.write_text("Monthly facts.\n", encoding="utf-8")
    records = [
        {
            "property_name": f"Test House {index} Public",
            "update_candidate": str(update),
        }
        for index in range(29)
    ]

    plan = build_plan(
        {
            "run_month": "2026-07",
            "reporting_cutoff_date": "2026-07-31",
            "authoritative_active_property_count": 32,
            "authoritative_reporting_target_count": 30,
            "records": records,
        }
    )

    assert plan["authoritative_active_property_count"] == 32
    assert plan["authoritative_reporting_target_count"] == 30
    assert plan["active_reporting_summary_count"] == 29
    assert plan["active_portfolio_summary_population_ok"] is False
    assert "active_reporting_summary_population_incomplete:29:expected=30" in plan["issues"]


def test_all_property_plan_accepts_32_physical_and_30_reporting_targets(tmp_path: Path) -> None:
    update = tmp_path / "Updates.md"
    update.write_text("Monthly facts.\n", encoding="utf-8")
    records = [
        {
            "property_name": f"Test House {index} Public",
            "update_candidate": str(update),
        }
        for index in range(30)
    ]

    plan = build_plan(
        {
            "run_month": "2026-07",
            "reporting_cutoff_date": "2026-07-31",
            "authoritative_active_property_count": 32,
            "authoritative_reporting_target_count": 30,
            "records": records,
        }
    )

    assert plan["active_portfolio_summary_population_ok"] is True
    assert plan["active_portfolio_summary_population_issues"] == []


def test_sold_property_cannot_replace_an_active_reporting_target(tmp_path: Path) -> None:
    update = tmp_path / "9919.md"
    update.write_text(
        "Financial detail:\n\n"
        "## Monthly Cash Position (2026-07)\n\n"
        "- ECO Net DAO Funds (spendable cash held by ECO): $0.00\n",
        encoding="utf-8",
    )
    filler = [
        {
            "property_name": f"Test House {index}",
            "update_candidate": str(update),
        }
        for index in range(30)
    ]

    plan = build_plan(
        {
            "run_month": "2026-07",
            "reporting_cutoff_date": "2026-07-31",
            "authoritative_active_property_count": 32,
            "authoritative_reporting_target_count": 30,
            "records": [
                *filler,
                {
                    "property_name": "9919 S Oglesby Ave",
                    "financial_summary_scope": "active_reporting_target",
                    "update_candidate": str(update),
                },
            ],
        }
    )

    assert plan["active_portfolio_summary_population_ok"] is True
    assert all(record["property_name"] != "9919 S Oglesby Ave" for record in plan["records"])


def test_lofty_listing_approval_only_hold_does_not_block_operator_review(tmp_path: Path) -> None:
    update = tmp_path / "review.md"
    update.write_text(
        "Financial detail:\n\n"
        "## Monthly Cash Position (2026-07)\n\n"
        "- ECO Net DAO Funds (spendable cash held by ECO): $0.00\n",
        encoding="utf-8",
    )
    records = [
        {"property_name": f"Test House {index}", "update_candidate": str(update)}
        for index in range(30)
    ]

    plan = build_plan(
        {
            "run_month": "2026-07",
            "reporting_cutoff_date": "2026-07-31",
            "authoritative_active_property_count": 32,
            "authoritative_reporting_target_count": 30,
            "records": records,
        },
        financial_patch_readiness={
            "status": "review",
            "blocked_count": 13,
            "approval_target_stale_count": 13,
            "candidate_packet_monthly_summary_issue_count": 0,
            "runtime_monthly_summary_issue_count": 0,
            "candidate_source_freshness_issue_count": 0,
            "guard_reconcile_required_count": 0,
        },
    )

    assert plan["status"] == "ok"
    assert plan["financial_review_blocked_record_count"] == 0
