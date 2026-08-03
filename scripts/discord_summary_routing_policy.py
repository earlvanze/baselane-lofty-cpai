#!/usr/bin/env python3
"""Authoritative Discord routing policy for monthly financial summaries."""

from __future__ import annotations

import re
from typing import Any


EARLCOIN_GUILD_ID = "1473153860376858756"
EARLCOIN_GUILD_NAME = "EARLCoin"
EARLCOIN_REVIEW_FORUM_ID = "1480241103528530141"
EARLCOIN_REVIEW_FORUM_NAME = "eco-systems-pm"
EARLCOIN_REVIEW_TARGET = f"channel:{EARLCOIN_REVIEW_FORUM_ID}"
EARLCOIN_REVIEW_DISCORD_ACCOUNT = "default"

LOFTY_GUILD_ID = "847877825373012018"
LOFTY_GUILD_NAME = "Lofty Investors"

REVIEW_DESTINATION_CLASS = "earlcoin_operator_review"
REVIEW_DESTINATION_PURPOSE = "operator_review"
REVIEW_PUBLICATION_STATE = "draft_review_only"
LOFTY_PUBLICATION_APPROVAL_SCOPE = "lofty_guild_financial_summary_publish"
DRAFT_REVIEW_PREFIX = "[DRAFT FOR REVIEW - NOT EMAILED]\n\n"

ACTIVE_PORTFOLIO_SUMMARY_POLICY_VERSION = "2026-08-02.2"
EXPECTED_ACTIVE_PHYSICAL_PROPERTY_COUNT = 32
EXPECTED_ACTIVE_REPORTING_TARGET_COUNT = 30
ACTIVE_PORTFOLIO_SUMMARY_POLICY = (
    "The July 2026 authority contains exactly 32 active physical properties and 30 grouped reporting targets. "
    "Publish one current-month financial summary for every reporting target. Selling properties remain active; "
    "only evidenced sold, closed, delisted, entity-only, or split-excluded rows are removed. Physical-property "
    "coverage and grouped reporting-target coverage are separate mandatory checks."
)

def active_portfolio_summary_population_issues(
    authoritative_property_count: object,
    authoritative_reporting_target_count: object,
    summary_count: object,
) -> list[str]:
    """Return fail-closed physical-roster and grouped-summary coverage violations."""
    try:
        property_count = int(authoritative_property_count)
    except (TypeError, ValueError):
        return ["active_property_count_missing_or_invalid"]
    try:
        reporting_target_count = int(authoritative_reporting_target_count)
    except (TypeError, ValueError):
        return ["active_reporting_target_count_missing_or_invalid"]
    try:
        actual_count = int(summary_count)
    except (TypeError, ValueError):
        return ["active_reporting_summary_count_missing_or_invalid"]

    issues: list[str] = []
    if property_count != EXPECTED_ACTIVE_PHYSICAL_PROPERTY_COUNT:
        issues.append(
            "active_physical_property_count_mismatch:"
            f"{property_count}:expected={EXPECTED_ACTIVE_PHYSICAL_PROPERTY_COUNT}"
        )
    if reporting_target_count != EXPECTED_ACTIVE_REPORTING_TARGET_COUNT:
        issues.append(
            "active_reporting_target_count_mismatch:"
            f"{reporting_target_count}:expected={EXPECTED_ACTIVE_REPORTING_TARGET_COUNT}"
        )
    if reporting_target_count > property_count:
        issues.append(
            "active_reporting_target_count_exceeds_physical_property_count:"
            f"{reporting_target_count}:physical={property_count}"
        )
    if actual_count != reporting_target_count:
        issues.append(
            f"active_reporting_summary_population_incomplete:{actual_count}:expected={reporting_target_count}"
        )
    return issues


def display_property_name(value: object) -> str:
    return re.sub(r"\s+Public\s*$", "", str(value or "").strip(), flags=re.IGNORECASE).strip()


def normalize_thread_name(value: object) -> str:
    text = display_property_name(value).lower().replace("&", "and")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def review_destination() -> dict[str, Any]:
    return {
        "destination_class": REVIEW_DESTINATION_CLASS,
        "destination_purpose": REVIEW_DESTINATION_PURPOSE,
        "publication_state": REVIEW_PUBLICATION_STATE,
        "guild_id": EARLCOIN_GUILD_ID,
        "guild_name": EARLCOIN_GUILD_NAME,
        "forum_id": EARLCOIN_REVIEW_FORUM_ID,
        "forum_name": EARLCOIN_REVIEW_FORUM_NAME,
        "target": EARLCOIN_REVIEW_TARGET,
        "discord_account_id": EARLCOIN_REVIEW_DISCORD_ACCOUNT,
        "human_approval_required_for_lofty_publication": True,
        "lofty_publication_approval_scope": LOFTY_PUBLICATION_APPROVAL_SCOPE,
        "lofty_publication_guild_id": LOFTY_GUILD_ID,
    }


def review_route(property_name: object) -> dict[str, Any]:
    thread_name = display_property_name(property_name)
    return {
        **review_destination(),
        "thread_name": thread_name,
        "thread_name_normalized": normalize_thread_name(thread_name),
        "route_matched": bool(thread_name),
    }


def _field_issue(
    issues: list[dict[str, Any]],
    *,
    scope: str,
    field: str,
    actual: object,
    expected: object,
    property_name: str = "",
) -> None:
    if actual == expected:
        return
    issues.append(
        {
            "scope": scope,
            "property_name": property_name or None,
            "field": field,
            "actual": actual,
            "expected": expected,
            "issue": f"{scope}.{field}_mismatch",
        }
    )


def review_route_issues(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Return route contract violations without performing external I/O."""
    issues: list[dict[str, Any]] = []
    expected_destination = review_destination()
    actual_destination = plan.get("review_destination")
    if not isinstance(actual_destination, dict):
        issues.append(
            {
                "scope": "plan",
                "property_name": None,
                "field": "review_destination",
                "actual": actual_destination,
                "expected": expected_destination,
                "issue": "plan.review_destination_missing_or_invalid",
            }
        )
    else:
        for field, expected in expected_destination.items():
            _field_issue(
                issues,
                scope="plan.review_destination",
                field=field,
                actual=actual_destination.get(field),
                expected=expected,
            )

    for field, expected in expected_destination.items():
        _field_issue(
            issues,
            scope="plan",
            field=field,
            actual=plan.get(field),
            expected=expected,
        )

    records = plan.get("records") if isinstance(plan.get("records"), list) else []
    seen_threads: dict[str, str] = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            issues.append(
                {
                    "scope": f"records[{index}]",
                    "property_name": None,
                    "field": "record",
                    "actual": record,
                    "expected": "object",
                    "issue": "record.invalid",
                }
            )
            continue
        property_name = str(record.get("property_name") or "").strip()
        expected_route = review_route(property_name)
        for field, expected in expected_route.items():
            _field_issue(
                issues,
                scope=f"records[{index}]",
                field=field,
                actual=record.get(field),
                expected=expected,
                property_name=property_name,
            )

        normalized_thread = str(record.get("thread_name_normalized") or "").strip()
        if not normalized_thread:
            issues.append(
                {
                    "scope": f"records[{index}]",
                    "property_name": property_name or None,
                    "field": "thread_name_normalized",
                    "actual": normalized_thread,
                    "expected": "nonempty unique property thread",
                    "issue": "record.thread_name_missing",
                }
            )
        elif normalized_thread in seen_threads:
            issues.append(
                {
                    "scope": f"records[{index}]",
                    "property_name": property_name or None,
                    "field": "thread_name_normalized",
                    "actual": normalized_thread,
                    "expected": "unique property thread",
                    "issue": "record.duplicate_thread_route",
                    "first_property_name": seen_threads[normalized_thread],
                }
            )
        else:
            seen_threads[normalized_thread] = property_name

        for guild_field in ("guild_id", "guildId"):
            if str(record.get(guild_field) or "") == LOFTY_GUILD_ID:
                issues.append(
                    {
                        "scope": f"records[{index}]",
                        "property_name": property_name or None,
                        "field": guild_field,
                        "actual": LOFTY_GUILD_ID,
                        "expected": EARLCOIN_GUILD_ID,
                        "issue": "record.lofty_guild_forbidden_for_review",
                    }
                )

    return issues


def review_route_ok(plan: dict[str, Any]) -> bool:
    return not review_route_issues(plan)


def review_delivery_report_issues(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate that a send receipt came from the EARLCoin review-only sender."""
    issues: list[dict[str, Any]] = []
    expected_destination = review_destination()
    actual_destination = report.get("review_destination")
    if not isinstance(actual_destination, dict):
        issues.append(
            {
                "scope": "send_report",
                "property_name": None,
                "field": "review_destination",
                "actual": actual_destination,
                "expected": expected_destination,
                "issue": "send_report.review_destination_missing_or_invalid",
            }
        )
    else:
        for field, expected in expected_destination.items():
            _field_issue(
                issues,
                scope="send_report.review_destination",
                field=field,
                actual=actual_destination.get(field),
                expected=expected,
            )

    for field, expected in expected_destination.items():
        _field_issue(
            issues,
            scope="send_report",
            field=field,
            actual=report.get(field),
            expected=expected,
        )

    records = report.get("results") if isinstance(report.get("results"), list) else []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            issues.append(
                {
                    "scope": f"results[{index}]",
                    "property_name": None,
                    "field": "record",
                    "actual": record,
                    "expected": "object",
                    "issue": "send_report.record_invalid",
                }
            )
            continue
        property_name = str(record.get("property_name") or "").strip()
        for field, expected in (
            ("guild_id", EARLCOIN_GUILD_ID),
            ("forum_id", EARLCOIN_REVIEW_FORUM_ID),
            ("forum_target", EARLCOIN_REVIEW_TARGET),
            ("discord_account_id", EARLCOIN_REVIEW_DISCORD_ACCOUNT),
        ):
            _field_issue(
                issues,
                scope=f"results[{index}]",
                field=field,
                actual=record.get(field),
                expected=expected,
                property_name=property_name,
            )

        target = str(record.get("target") or "").strip()
        thread_id = str(record.get("thread_id") or "").strip()
        has_receipt = isinstance(record.get("header_receipt"), dict) or isinstance(
            record.get("body_receipt"), dict
        )
        if has_receipt and (not thread_id or target != f"channel:{thread_id}"):
            issues.append(
                {
                    "scope": f"results[{index}]",
                    "property_name": property_name or None,
                    "field": "target",
                    "actual": target,
                    "expected": "channel:<EARLCoin review thread_id>",
                    "issue": "send_report.receipt_thread_target_invalid",
                }
            )

    return issues
