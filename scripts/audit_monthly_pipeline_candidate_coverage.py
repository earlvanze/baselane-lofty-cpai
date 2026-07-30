#!/usr/bin/env python3
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from coownership_mortgage_policy import P_AND_I_DAO_PROPERTIES, normalize_policy_key
from transfer_report_digest import stable_transfer_report_digest


def load_json(path: Path) -> dict:
    if not path.exists():
        return {"status": "missing", "path": str(path)}
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def count_discord_mapped(properties: list[dict]) -> int:
    return sum(1 for item in properties if str(item.get("discord_channel_id") or "").strip())


def property_names(payload: dict, list_key: str) -> set[str]:
    records = payload.get(list_key)
    if not isinstance(records, list):
        return set()
    names: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        name = (
            record.get("property_name")
            or record.get("property")
            or record.get("name")
            or record.get("input_property_name")
        )
        key = normalize_policy_key(name)
        if key:
            names.add(key)
    return names


def coverage_key(value: object) -> str:
    key = normalize_policy_key(value)
    for suffix in (" albany", " denver"):
        if key.endswith(suffix):
            return key[: -len(suffix)].strip()
    return key


def coverage_names(payload: dict, list_key: str) -> set[str]:
    records = payload.get(list_key)
    if not isinstance(records, list):
        return set()
    names: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        status = str(record.get("status") or "")
        if status.startswith(("excluded_", "skipped_")) or record.get("exclude_source"):
            continue
        name = (
            record.get("property_name")
            or record.get("property")
            or record.get("name")
            or record.get("input_property_name")
        )
        key = coverage_key(name)
        if key:
            names.add(key)
    return names


def excluded_coverage_names(payload: dict, list_key: str) -> set[str]:
    records = payload.get(list_key)
    if not isinstance(records, list):
        return set()
    names: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        status = str(record.get("status") or "")
        if not (status.startswith(("excluded_", "skipped_")) or record.get("exclude_source")):
            continue
        name = (
            record.get("property_name")
            or record.get("property")
            or record.get("name")
            or record.get("input_property_name")
        )
        key = coverage_key(name)
        if key:
            names.add(key)
    return names


def publish_effective_count(payload: dict) -> int | None:
    for key in ("publish_result_count", "updates_publish_result_count"):
        value = int_or_none(payload.get(key))
        if value is not None:
            return value
    return int_or_none(payload.get("property_count"))


def owner_effective_names(payload: dict) -> set[str]:
    names = coverage_names(payload, "records")
    previews = payload.get("native_lofty_owner_email_previews")
    if isinstance(previews, list):
        for preview in previews:
            if not isinstance(preview, dict):
                continue
            key = coverage_key(preview.get("property_name"))
            if key:
                names.add(key)
    held_properties = payload.get("native_lofty_owner_email_idempotency_held_properties")
    if isinstance(held_properties, list):
        for property_name in held_properties:
            key = coverage_key(property_name)
            if key:
                names.add(key)
    return names


def discord_effective_names(payload: dict) -> set[str]:
    names: set[str] = set()
    for list_key in ("records", "send_records", "plan", "results"):
        names.update(coverage_names(payload, list_key))
    return names


def int_or_none(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def owner_packet_reviewed_mcp_coverage(payload: dict) -> dict:
    property_count = int_or_none(payload.get("property_count")) or 0
    available_count = int_or_none(payload.get("available_property_count")) or 0
    issue_count = int_or_none(payload.get("issue_count")) or 0
    missing_summary_count = int_or_none(payload.get("monthly_financial_summary_missing_property_count")) or 0
    full_history_leak_count = int_or_none(payload.get("full_history_leak_count")) or 0
    full_history_guard_issue_count = int_or_none(payload.get("full_history_guard_issue_count")) or 0
    body_guard_issue_count = int_or_none(payload.get("body_guard_issue_count")) or 0
    unsafe_preview_packet_count = int_or_none(payload.get("unsafe_preview_packet_count")) or 0
    ok = bool(
        payload.get("status") == "ok"
        and property_count > 0
        and available_count == property_count
        and issue_count == 0
        and missing_summary_count == 0
        and full_history_leak_count == 0
        and full_history_guard_issue_count == 0
        and body_guard_issue_count == 0
        and unsafe_preview_packet_count == 0
    )
    return {
        "ok": ok,
        "property_count": property_count,
        "available_property_count": available_count,
        "issue_count": issue_count,
        "monthly_financial_summary_missing_property_count": missing_summary_count,
        "full_history_leak_count": full_history_leak_count,
        "full_history_guard_issue_count": full_history_guard_issue_count,
        "body_guard_issue_count": body_guard_issue_count,
        "unsafe_preview_packet_count": unsafe_preview_packet_count,
    }


def owner_effective_count(payload: dict) -> tuple[int | None, str]:
    reviewed = owner_packet_reviewed_mcp_coverage(payload)
    if reviewed["ok"]:
        return reviewed["available_property_count"], "reviewed_lofty_pm_mcp_packet"
    return int_or_none(payload.get("native_lofty_owner_email_property_count")), "native_lofty_owner_email"


def owner_financial_review_hold_names(payload: dict) -> set[str]:
    held = payload.get("native_lofty_owner_email_financially_held_properties")
    if not isinstance(held, list):
        return set()
    return {coverage_key(name) for name in held if coverage_key(name)}


def discord_held_financial_review_records(payload: dict) -> list[dict]:
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    held = []
    for result in results:
        if not isinstance(result, dict):
            continue
        if result.get("status") != "held_financial_review" and not result.get("financial_review_blockers"):
            continue
        held.append(
            {
                "property_name": result.get("property_name"),
                "target": result.get("target"),
                "financial_review_blockers": result.get("financial_review_blockers")
                if isinstance(result.get("financial_review_blockers"), list)
                else [],
            }
        )
    return held


def transfer_property_cash_review_names(payload: dict) -> set[str]:
    records = payload.get("property_cash_review_details")
    if not isinstance(records, list):
        return set()
    names: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        key = coverage_key(record.get("property") or record.get("property_name"))
        if key:
            names.add(key)
    return names


def policy_ignore_names(payload: dict) -> set[str]:
    names: set[str] = set()
    for list_key in ("sold_ignore_listing_updates", "operational_ignore_listing_updates"):
        records = payload.get(list_key)
        if not isinstance(records, list):
            continue
        for record in records:
            if isinstance(record, str):
                name = record
            elif isinstance(record, dict):
                name = (
                    record.get("property_name")
                    or record.get("property")
                    or record.get("name")
                    or record.get("address")
                )
            else:
                name = None
            key = coverage_key(name)
            if key:
                names.add(key)
    return names


def runtime_excluded_names(payload: dict) -> set[str]:
    names: set[str] = set()
    for list_key in ("records", "properties"):
        names.update(excluded_coverage_names(payload, list_key))
    return names


def surface_contains_property(names: set[str], property_key: str) -> bool:
    return any(property_key == name or property_key in name or name in property_key for name in names)


def required_property_coverage(
    required_properties: set[str],
    surfaces: dict[str, set[str]],
    held_properties: set[str] | None = None,
) -> list[dict]:
    coverage = []
    held_properties = held_properties or set()
    for property_name in sorted(required_properties):
        key = normalize_policy_key(property_name)
        held = surface_contains_property(held_properties, key)
        missing_from = [
            surface for surface, names in sorted(surfaces.items()) if names and not surface_contains_property(names, key)
        ]
        if held:
            missing_from = []
        coverage.append(
            {
                "property": property_name,
                "property_key": key,
                "present_in": sorted(surface for surface, names in surfaces.items() if surface_contains_property(names, key)),
                "missing_from": missing_from,
                "held_by_property_cash_review": held,
            }
        )
    return coverage


def build_report(args: argparse.Namespace) -> dict:
    route_config = load_json(args.property_update_map)
    review_candidate_packet = load_json(args.review_candidate_packet)
    publish = load_json(args.lofty_publish_report)
    owner_packet = load_json(args.owner_email_packet)
    guild_post = load_json(args.guild_post_report)
    discord_send = load_json(args.discord_send_report)
    transfer = load_json(args.transfer_report)
    telegram = load_json(args.telegram_send_report)
    runtime_map_path = getattr(args, "runtime_map", None)
    runtime_map = load_json(runtime_map_path) if runtime_map_path else {}
    listing_update_policy_path = getattr(args, "listing_update_policy", None)
    listing_update_policy = load_json(listing_update_policy_path) if listing_update_policy_path else {}
    listing_update_ignore_names = policy_ignore_names(listing_update_policy) | runtime_excluded_names(runtime_map)

    route_properties = route_config.get("properties") if isinstance(route_config.get("properties"), list) else []
    publish_excluded = publish.get("excluded_records") or []
    excluded_names = [
        item.get("property_name") or item.get("property") or item.get("name")
        for item in publish_excluded
        if item.get("property_name") or item.get("property") or item.get("name")
    ]

    guild_target = guild_post.get("target") or guild_post.get("target_channel")
    discord_target = discord_send.get("target")
    target_match = bool(guild_target and discord_target and guild_target == discord_target)

    informational = []
    mismatches = []
    route_count = len(route_properties)
    runtime_publish_count = int_or_none(publish.get("property_count"))
    publish_count = publish_effective_count(publish)
    owner_count, owner_count_source = owner_effective_count(owner_packet)
    owner_reviewed_mcp_coverage = owner_packet_reviewed_mcp_coverage(owner_packet)
    candidate_names = {
        coverage_key(record.get("property_name"))
        for record in review_candidate_packet.get("records", [])
        if (
            isinstance(record, dict)
            and record.get("update_candidate")
            and isinstance(record.get("monthly_financial_summary"), dict)
            and record.get("monthly_financial_summary")
        )
    } if isinstance(review_candidate_packet.get("records"), list) else set()
    candidate_names.discard("")
    candidate_names = {
        name for name in candidate_names if not surface_contains_property(listing_update_ignore_names, name)
    }
    publish_names = (
        coverage_names(publish, "publish_results")
        or coverage_names(publish, "records")
        or candidate_names
    )
    publish_names = {
        name for name in publish_names if not surface_contains_property(listing_update_ignore_names, name)
    }
    if publish_count == 0 and candidate_names and publish.get("publish_attempted") is not True:
        publish_count = len(candidate_names)
    raw_owner_names = owner_effective_names(owner_packet)
    owner_names = {
        name for name in raw_owner_names if not surface_contains_property(listing_update_ignore_names, name)
    }
    owner_financial_review_hold_names_set = {
        name
        for name in owner_financial_review_hold_names(owner_packet)
        if not surface_contains_property(listing_update_ignore_names, name)
    }
    effective_publish_count = len(publish_names) if publish_names else publish_count
    effective_owner_count = len(owner_names) if owner_names else owner_count
    discord_names = discord_effective_names(discord_send)
    discord_record_count = int_or_none(discord_send.get("record_count"))
    discord_sent_or_verified_count = int_or_none(
        discord_send.get("sent_or_verified_count")
        if discord_send.get("sent_or_verified_count") is not None
        else discord_send.get("sent_or_would_send_count")
    )
    discord_failed_count = int_or_none(discord_send.get("failed_count")) or 0
    discord_missing_financial_summary_count = int_or_none(discord_send.get("missing_financial_summary_count")) or 0
    discord_unmapped_count = int_or_none(discord_send.get("unmapped_count")) or 0
    discord_held_financial_reviews = discord_held_financial_review_records(discord_send)
    discord_held_financial_review_names = {
        coverage_key(item.get("property_name"))
        for item in discord_held_financial_reviews
        if coverage_key(item.get("property_name"))
    }
    discord_active_candidate_names = discord_names & publish_names if publish_names else discord_names
    discord_active_candidate_held_names = discord_held_financial_review_names & publish_names
    transfer_review_held_names = transfer_property_cash_review_names(transfer)
    telegram_dry_run = telegram.get("dry_run") is True
    telegram_send_ok = telegram.get("telegram_send_ok") is True
    transfer_digest = stable_transfer_report_digest(args.transfer_report)
    telegram_transfer_digest = str(
        telegram.get("transfer_report_digest")
        or telegram.get("current_transfer_report_digest")
        or ""
    ).strip()
    telegram_digest_matches_current = (
        telegram.get("transfer_report_digest_matches_current") is True
        and bool(transfer_digest)
        and telegram_transfer_digest == transfer_digest
    )
    transfer_source_blockers = transfer.get("source_blockers") if isinstance(transfer.get("source_blockers"), list) else []
    transfer_bank_actions_final = transfer.get("bank_transfer_actions_final")
    transfer_final = (
        transfer_bank_actions_final
        if transfer_bank_actions_final is not None
        else transfer.get("recommended_send_to_lofty_total_is_final")
    )
    required_p_and_i_properties = {
        property_name
        for property_name in P_AND_I_DAO_PROPERTIES
        if not surface_contains_property(listing_update_ignore_names, coverage_key(property_name))
    }
    required_p_and_i_coverage = required_property_coverage(
        required_p_and_i_properties,
        {
            "lofty_publish_candidate": publish_names,
            "transfer_reconciliation": property_names(transfer, "rows"),
        },
        held_properties=transfer_review_held_names,
    )
    required_p_and_i_missing = [
        item for item in required_p_and_i_coverage if item["missing_from"]
    ]

    if route_count and runtime_publish_count is not None and route_count != runtime_publish_count:
        informational.append(
            {
                "kind": "route_inventory_vs_monthly_publish_set",
                "route_inventory_property_count": route_count,
                "monthly_publish_property_count": runtime_publish_count,
                "explanation": "Discord route config is a channel inventory; monthly publish count excludes records gated by the Lofty/Yhome publish index.",
            }
        )
    owner_financial_review_holds_publish_set = bool(publish_names) and publish_names <= owner_financial_review_hold_names_set
    if owner_financial_review_holds_publish_set:
        informational.append(
            {
                "kind": "monthly_owner_email_financial_review_hold",
                "monthly_publish_property_count": effective_publish_count,
                "financially_held_property_count": len(owner_financial_review_hold_names_set),
                "covered_publish_property_count": len(publish_names),
                "explanation": "The active monthly publish set is intentionally held by the portfolio financial-review gate; this is not an owner-email coverage mismatch.",
            }
        )
    elif effective_publish_count is not None and effective_owner_count is not None and effective_publish_count != effective_owner_count:
        mismatch = {
            "kind": "monthly_publish_vs_owner_email_packet_coverage",
            "monthly_publish_property_count": effective_publish_count,
            "owner_email_property_count": effective_owner_count,
            "owner_email_property_count_source": owner_count_source,
            "native_owner_email_property_count": owner_packet.get("native_lofty_owner_email_property_count"),
        }
        excluded_owner_names = sorted(raw_owner_names - owner_names)
        if excluded_owner_names:
            mismatch["excluded_owner_email_property_names"] = excluded_owner_names
        mismatches.append(mismatch)
    if publish_names and owner_names and publish_names != owner_names:
        mismatches.append(
            {
                "kind": "monthly_publish_vs_native_owner_email_identity_coverage",
                "missing_from_native_owner_email": sorted(publish_names - owner_names),
                "extra_in_native_owner_email": sorted(owner_names - publish_names),
            }
        )
    if discord_send.get("mode") == "all_plan":
        discord_active_candidate_count = len(discord_active_candidate_names)
        discord_coverage_count = (
            discord_active_candidate_count
            if discord_names
            else discord_record_count
        )
        if (
            effective_publish_count is not None
            and discord_coverage_count is not None
            and discord_coverage_count != effective_publish_count
        ):
            mismatches.append(
                {
                    "kind": "monthly_publish_vs_discord_all_plan_coverage",
                    "monthly_publish_property_count": effective_publish_count,
                    "discord_active_candidate_record_count": discord_coverage_count,
                    "discord_all_plan_record_count": discord_record_count,
                }
            )
        if publish_names and discord_active_candidate_names != publish_names:
            mismatches.append(
                {
                    "kind": "monthly_publish_vs_discord_all_plan_identity_coverage",
                    "missing_from_discord_all_plan": sorted(publish_names - discord_active_candidate_names),
                    "extra_in_discord_all_plan": sorted(discord_active_candidate_names - publish_names),
                }
            )
        if (
            discord_record_count is not None
            and discord_sent_or_verified_count is not None
            and discord_sent_or_verified_count != discord_record_count
        ):
            mismatches.append(
                {
                    "kind": "discord_all_plan_send_proof_incomplete",
                    "discord_all_plan_record_count": discord_record_count,
                    "sent_or_verified_count": discord_sent_or_verified_count,
                    "held_financial_review_count": len(discord_held_financial_reviews),
                    "held_financial_review_properties": [
                        item.get("property_name") for item in discord_held_financial_reviews if item.get("property_name")
                    ],
                    "held_financial_review_blockers": [
                        blocker
                        for item in discord_held_financial_reviews
                        for blocker in item.get("financial_review_blockers", [])
                    ][:25],
                    "active_monthly_candidate_count": effective_publish_count,
                    "active_monthly_candidate_held_financial_review_count": len(discord_active_candidate_held_names),
                }
            )
        if discord_failed_count:
            mismatches.append(
                {
                    "kind": "discord_all_plan_failed_records",
                    "failed_count": discord_failed_count,
                }
            )
        if discord_unmapped_count:
            mismatches.append(
                {
                    "kind": "discord_all_plan_unmapped_properties",
                    "unmapped_count": discord_unmapped_count,
                }
            )
        if discord_missing_financial_summary_count:
            mismatches.append(
                {
                    "kind": "discord_all_plan_missing_financial_summary",
                    "missing_financial_summary_count": discord_missing_financial_summary_count,
                }
            )
    if transfer_final is True and not (telegram_send_ok or telegram_dry_run):
        mismatches.append(
            {
                "kind": "transfer_reconciliation_final_without_telegram_delivery",
                "transfer_reconciliation_status": transfer.get("status"),
                "bank_transfer_actions_final": transfer.get("bank_transfer_actions_final"),
                "recommended_send_to_lofty_total_is_final": transfer.get("recommended_send_to_lofty_total_is_final"),
                "telegram_status": telegram.get("status"),
                "telegram_send_ok": telegram.get("telegram_send_ok"),
                "telegram_dry_run": telegram.get("dry_run"),
            }
        )
    if transfer_final is True and (telegram_send_ok or telegram_dry_run) and not telegram_digest_matches_current:
        mismatches.append(
            {
                "kind": "transfer_reconciliation_final_with_stale_telegram_delivery",
                "transfer_reconciliation_status": transfer.get("status"),
                "bank_transfer_actions_final": transfer.get("bank_transfer_actions_final"),
                "recommended_send_to_lofty_total_is_final": transfer.get("recommended_send_to_lofty_total_is_final"),
                "telegram_status": telegram.get("status"),
                "telegram_send_ok": telegram.get("telegram_send_ok"),
                "telegram_dry_run": telegram.get("dry_run"),
                "transfer_report_digest": transfer_digest,
                "telegram_transfer_report_digest": telegram_transfer_digest or None,
                "telegram_transfer_report_digest_matches_current": telegram_digest_matches_current,
            }
        )
    if (
        transfer_final is not True
        and telegram_send_ok
        and not telegram_dry_run
    ):
        mismatches.append(
            {
                "kind": "transfer_reconciliation_not_final_with_live_telegram_delivery",
                "transfer_reconciliation_status": transfer.get("status"),
                "bank_transfer_actions_final": transfer.get("bank_transfer_actions_final"),
                "recommended_send_to_lofty_total_is_final": transfer.get("recommended_send_to_lofty_total_is_final"),
                "telegram_status": telegram.get("status"),
                "telegram_send_ok": telegram.get("telegram_send_ok"),
                "telegram_dry_run": telegram.get("dry_run"),
                "transfer_report_digest": transfer_digest,
                "telegram_transfer_report_digest": telegram_transfer_digest or None,
                "telegram_transfer_report_digest_matches_current": telegram_digest_matches_current,
            }
        )
    if (
        transfer_final is not True
        and (telegram_send_ok or telegram_dry_run)
        and telegram_transfer_digest
        and not telegram_digest_matches_current
    ):
        mismatches.append(
            {
                "kind": "transfer_reconciliation_telegram_delivery_stale",
                "transfer_reconciliation_status": transfer.get("status"),
                "bank_transfer_actions_final": transfer.get("bank_transfer_actions_final"),
                "recommended_send_to_lofty_total_is_final": transfer.get("recommended_send_to_lofty_total_is_final"),
                "telegram_status": telegram.get("status"),
                "telegram_send_ok": telegram.get("telegram_send_ok"),
                "telegram_dry_run": telegram.get("dry_run"),
                "transfer_report_digest": transfer_digest,
                "telegram_transfer_report_digest": telegram_transfer_digest,
                "telegram_transfer_report_digest_matches_current": telegram_digest_matches_current,
            }
        )
    if (guild_target or discord_target) and discord_send.get("mode") != "all_plan":
        if not target_match:
            mismatches.append(
                {
                    "kind": "discord_target_mismatch",
                    "guild_post_target": guild_target,
                    "discord_send_target": discord_target,
                }
            )
    if required_p_and_i_missing:
        mismatches.append(
            {
                "kind": "required_p_and_i_dao_monthly_surface_coverage",
                "missing": required_p_and_i_missing,
            }
        )

    status = "ok" if not mismatches else "review"

    return {
        "generated_at": iso_z(),
        "status": status,
        "mismatch_count": len(mismatches),
        "informational_count": len(informational),
        "input_digests": {
            "property_update_map": sha256_file(args.property_update_map),
            "review_candidate_packet": sha256_file(args.review_candidate_packet),
            "lofty_publish_report": sha256_file(args.lofty_publish_report),
            "runtime_map": sha256_file(runtime_map_path) if runtime_map_path else None,
            "owner_email_packet": sha256_file(args.owner_email_packet),
            "discord_send_report": sha256_file(args.discord_send_report),
            "transfer_report": transfer_digest,
            "telegram_send_report": sha256_file(args.telegram_send_report),
        },
        "property_update_map": {
            "path": str(args.property_update_map),
            "property_count": route_count,
            "discord_mapped_count": count_discord_mapped(route_properties),
            "coverage_ok": bool(route_count and count_discord_mapped(route_properties) == route_count),
            "role": "discord_route_inventory_not_monthly_active_send_set",
        },
        "lofty_publish": {
            "status": publish.get("status"),
            "property_count": runtime_publish_count,
            "effective_monthly_candidate_count": effective_publish_count,
            "effective_monthly_candidate_source": (
                "publish_results"
                if coverage_names(publish, "publish_results")
                else ("review_candidate_packet" if candidate_names else "runtime_records")
            ),
            "review_candidate_packet_status": review_candidate_packet.get("status"),
            "review_candidate_update_candidate_count": len(candidate_names),
            "publish_result_count": publish.get("publish_result_count"),
            "failed_count": publish.get("failed_count"),
            "excluded_property_count": publish.get("excluded_property_count"),
            "excluded_property_names": excluded_names,
            "runtime_excluded_property_names": sorted(runtime_excluded_names(runtime_map)),
            "effective_ignore_property_names": sorted(listing_update_ignore_names),
            "effective_monthly_candidate_names": sorted(publish_names),
            "raw_publish_effective_count": publish_count,
        },
        "owner_email_packet": {
            "status": owner_packet.get("status"),
            "property_count": owner_packet.get("property_count"),
            "available_property_count": owner_packet.get("available_property_count"),
            "effective_property_count": effective_owner_count,
            "effective_property_count_source": owner_count_source,
            "reviewed_lofty_pm_mcp_packet_coverage_ok": owner_reviewed_mcp_coverage["ok"],
            "reviewed_lofty_pm_mcp_packet_coverage": owner_reviewed_mcp_coverage,
            "native_property_count": owner_packet.get("native_lofty_owner_email_property_count"),
            "native_coverage_ok": owner_packet.get("native_lofty_owner_email_property_coverage_ok"),
            "non_native_packet_count": owner_packet.get("non_native_packet_count"),
            "native_property_names": sorted(owner_names),
            "excluded_native_property_names": sorted(raw_owner_names - owner_names),
            "coverage_policy": (
                "Monthly publish coverage is satisfied by the reviewed signal-only lofty-pm owner-update "
                "packet. Disabled native Lofty owner email counters are diagnostic only because that native path "
                "can send the full saved updates field."
            ),
        },
        "discord_property_update": {
            "guild_post_status": guild_post.get("status"),
            "guild_post_target": guild_target,
            "send_status": discord_send.get("status"),
            "send_target": discord_target,
            "mode": discord_send.get("mode"),
            "record_count": discord_record_count,
            "sent_or_verified_count": discord_sent_or_verified_count,
            "failed_count": discord_failed_count,
            "unmapped_count": discord_unmapped_count,
            "missing_financial_summary_count": discord_missing_financial_summary_count,
            "held_financial_review_count": len(discord_held_financial_reviews),
            "held_financial_review_properties": [
                item.get("property_name") for item in discord_held_financial_reviews if item.get("property_name")
            ],
            "held_financial_review_blockers": [
                blocker
                for item in discord_held_financial_reviews
                for blocker in item.get("financial_review_blockers", [])
            ][:25],
            "property_names": sorted(discord_names),
            "active_monthly_candidate_record_count": len(discord_active_candidate_names),
            "active_monthly_candidate_held_financial_review_count": len(discord_active_candidate_held_names),
            "target_match": target_match,
        },
        "transfer_reconciliation": {
            "status": transfer.get("status"),
            "ready_to_send_property_count": transfer.get("ready_to_send_property_count"),
            "held_property_count": transfer.get("held_property_count"),
            "recommended_send_to_lofty_total": transfer.get("recommended_send_to_lofty_total"),
            "recommended_send_to_lofty_total_is_final": transfer.get("recommended_send_to_lofty_total_is_final"),
            "bank_transfer_actions_final": transfer.get("bank_transfer_actions_final"),
            "effective_transfer_final": transfer_final,
            "eco_cash_shortfall_total": transfer.get("eco_cash_shortfall_total"),
            "source_blocker_count": len(transfer_source_blockers),
            "source_blockers": transfer_source_blockers[:25],
        },
        "required_p_and_i_dao_coverage": required_p_and_i_coverage,
        "required_p_and_i_dao_missing_count": len(required_p_and_i_missing),
        "required_p_and_i_dao_policy_excluded_properties": sorted(
            property_name
            for property_name in P_AND_I_DAO_PROPERTIES
            if surface_contains_property(listing_update_ignore_names, coverage_key(property_name))
        ),
        "listing_update_policy": {
            "path": str(listing_update_policy_path) if listing_update_policy_path else None,
            "ignored_property_count": len(listing_update_ignore_names),
            "ignored_property_keys": sorted(listing_update_ignore_names),
        },
        "telegram_reconciliation": {
            "status": telegram.get("status"),
            "telegram_send_ok": telegram.get("telegram_send_ok"),
            "dry_run": telegram.get("dry_run"),
            "chunk_count": telegram.get("chunk_count"),
            "transfer_report_digest": telegram_transfer_digest or None,
            "current_transfer_report_digest": transfer_digest,
            "transfer_report_digest_matches_current": telegram_digest_matches_current,
            "bank_transfer_actions_final": transfer.get("bank_transfer_actions_final"),
            "effective_transfer_final": transfer_final,
        },
        "mismatches": mismatches,
        "informational": informational,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--property-update-map", type=Path, default=Path("skills/lofty-pm/config/property_update_map.json"))
    parser.add_argument("--review-candidate-packet", type=Path, default=Path("reports/baselane_financials_monthly_review_candidate_packet.json"))
    parser.add_argument("--lofty-publish-report", type=Path, default=Path("reports/baselane_financials_monthly_lofty_pm_publish.json"))
    parser.add_argument("--owner-email-packet", type=Path, default=Path("reports/baselane_monthly_owner_email_packet.json"))
    parser.add_argument("--runtime-map", type=Path, default=Path("reports/baselane_financials_monthly_lofty_pm_runtime_map.json"))
    parser.add_argument("--guild-post-report", type=Path, default=Path("reports/baselane_financials_monthly_guild_test_post.json"))
    parser.add_argument("--discord-send-report", type=Path, default=Path("reports/baselane_financials_monthly_discord_property_update_send.json"))
    parser.add_argument("--transfer-report", type=Path, default=Path("reports/baselane_lofty_transfer_requirements.json"))
    parser.add_argument("--telegram-send-report", type=Path, default=Path("reports/baselane_lofty_transfer_requirements_telegram_send.json"))
    parser.add_argument("--listing-update-policy", type=Path, default=Path("config/lofty_listing_update_policy.json"))
    parser.add_argument("--report", type=Path, default=Path("reports/baselane_monthly_pipeline_candidate_coverage_audit.json"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_report(args)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(args.report), "mismatch_count": len(report["mismatches"])}, indent=2))
    return 0 if report["status"] in {"ok", "review"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
