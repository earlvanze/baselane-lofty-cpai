#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def load_active_roster_scope(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {
            "status": "not_configured",
            "path": None,
            "physical_property_count": None,
            "portfolio_reporting_target_count": None,
            "records": [],
            "issues": [],
        }
    if not path.is_file():
        return {
            "status": "missing",
            "path": str(path),
            "physical_property_count": None,
            "portfolio_reporting_target_count": None,
            "records": [],
            "issues": [f"active property roster missing: {path}"],
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "status": "unreadable",
            "path": str(path),
            "physical_property_count": None,
            "portfolio_reporting_target_count": None,
            "records": [],
            "issues": [f"active property roster unreadable: {path}: {exc}"],
        }
    if not isinstance(payload, dict):
        return {
            "status": "unreadable",
            "path": str(path),
            "physical_property_count": None,
            "portfolio_reporting_target_count": None,
            "records": [],
            "issues": [f"active property roster is not a JSON object: {path}"],
        }

    issues: list[str] = []
    if payload.get("status") != "ok":
        issues.append(f"active property roster status is not ok: {payload.get('status') or 'missing'}")
    physical_count = _positive_int(
        payload.get("authoritative_active_property_count") or payload.get("physical_property_count")
    )
    reporting_count = _positive_int(
        payload.get("authoritative_reporting_target_count") or payload.get("reporting_target_count")
    )
    if physical_count is None:
        issues.append("active property roster has no positive physical-property count")
    if reporting_count is None:
        issues.append("active property roster has no positive reporting-target count")
    records = [record for record in payload.get("records") or [] if isinstance(record, dict)]
    return {
        "status": "ok" if not issues else "review",
        "path": str(path),
        "physical_property_count": physical_count,
        "portfolio_reporting_target_count": reporting_count,
        "records": records,
        "issues": issues,
    }


def validate_full_reporting_scope(
    scope: dict[str, Any],
    selected_reporting_target_count: int,
    *,
    targeted: bool,
) -> list[str]:
    issues = list(scope.get("issues") or [])
    expected = scope.get("portfolio_reporting_target_count")
    if not targeted and expected is not None and selected_reporting_target_count != expected:
        issues.append(
            "active reporting scope mismatch: "
            f"selected={selected_reporting_target_count}, authoritative={expected}"
        )
    return issues


def split_native_live_targets(
    targets: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    native: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    for target in targets:
        if str(target.get("lofty_property_id") or "").strip():
            native.append(target)
            continue
        record = dict(target)
        record.update(
            {
                "status": "unavailable_no_live_property_id",
                "native_live_action_available": False,
                "nonblocking_scope": "native_lofty_listing_actions_only",
                "accounting_and_investor_reporting_included": True,
                "reason": (
                    "No native Lofty property ID is mapped; retain this target in the monthly accounting "
                    "and investor-reporting scope while skipping only native Lofty listing actions."
                ),
            }
        )
        unavailable.append(record)
    return native, unavailable


def enrich_targets_from_active_roster(
    targets: list[dict[str, Any]],
    scope: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Overlay canonical IDs and current manager availability onto index targets."""
    roster_records = [record for record in scope.get("records") or [] if isinstance(record, dict)]
    if not roster_records:
        return [dict(target) for target in targets], []

    by_path: dict[str, dict[str, Any]] = {}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for record in roster_records:
        path = str(record.get("property_path") or "").strip()
        if path:
            by_path[str(Path(path))] = record
        aliases = [
            record.get("property_name"),
            record.get("managed_name"),
            Path(path).name if path else None,
            *(record.get("physical_addresses") or []),
        ]
        for alias in aliases:
            normalized = _normalize_name(alias)
            if normalized:
                by_name.setdefault(normalized, []).append(record)

    enriched: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    for target in targets:
        target_path = str(Path(str(target.get("property_path") or "")))
        record = by_path.get(target_path)
        if record is None:
            candidates: list[dict[str, Any]] = []
            for alias in (
                target.get("property_name"),
                target.get("managed_name"),
                Path(target_path).name if target_path else None,
            ):
                candidates.extend(by_name.get(_normalize_name(alias), []))
            unique = {id(candidate): candidate for candidate in candidates}
            if len(unique) == 1:
                record = next(iter(unique.values()))

        result = dict(target)
        if record is None:
            result["active_roster_match_status"] = "unmatched"
            unmatched.append(result)
            enriched.append(result)
            continue

        result.update(
            {
                "active_roster_match_status": "matched",
                "lofty_property_id": str(record.get("lofty_property_id") or "").strip() or None,
                "lofty_live_mutation_available": record.get("lofty_live_mutation_available"),
                "lofty_manager_status": record.get("lofty_manager_status"),
                "physical_property_count": record.get("physical_property_count"),
            }
        )
        enriched.append(result)
    return enriched, unmatched


def partition_current_manager_targets(
    targets: list[dict[str, Any]],
    *,
    live_property_ids: set[str] | None = None,
    mutation_ready_property_ids: set[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Separate reporting, readable, and mutation-ready Lofty manager scopes."""
    captureable: list[dict[str, Any]] = []
    actionable: list[dict[str, Any]] = []
    known_id: list[dict[str, Any]] = []
    manager_unavailable: list[dict[str, Any]] = []
    mutation_unavailable: list[dict[str, Any]] = []
    no_id: list[dict[str, Any]] = []

    for target in targets:
        property_id = str(target.get("lofty_property_id") or "").strip()
        if not property_id:
            no_id.append(_nonblocking_record(target, "not_applicable_no_lofty_property_id", (
                "No native Lofty property ID is mapped. Monthly accounting and investor reporting remain in scope; "
                "only native Lofty listing actions are unavailable."
            )))
            continue

        known_id.append(target)
        live_readable = live_property_ids is None or property_id in live_property_ids
        if not live_readable:
            manager_unavailable.append(
                _nonblocking_record(
                    target,
                    "not_applicable_not_current_manager_property",
                    (
                        "The known Lofty property ID is absent from the current manager-property response. Monthly "
                        "accounting and investor reporting remain in scope; authenticated listing actions are unavailable."
                    ),
                )
            )
            continue

        if mutation_ready_property_ids is None:
            mutation_ready = target.get("lofty_live_mutation_available") is not False
        else:
            mutation_ready = property_id in mutation_ready_property_ids
        readable_target = dict(target)
        readable_target["native_live_action_available"] = mutation_ready
        readable_target["live_capture_guard_applicable"] = mutation_ready
        captureable.append(readable_target)
        if mutation_ready:
            actionable.append(readable_target)
            continue
        mutation_unavailable.append(
            {
                **readable_target,
                "mutation_unavailable_reason": (
                    "The property is readable in get-manager-properties but has no ready property-manager entry. "
                    "Its accounting and investor reporting remain in scope; listing mutation guards are non-applicable."
                ),
            }
        )

    return {
        "captureable": captureable,
        "actionable": actionable,
        "known_id": known_id,
        "manager_unavailable": manager_unavailable,
        "mutation_unavailable": mutation_unavailable,
        "no_id": no_id,
    }


def live_manager_mutation_ready(record: dict[str, Any]) -> bool:
    entry = record.get("plEntry") if isinstance(record.get("plEntry"), dict) else {}
    status = str(entry.get("status") or record.get("status") or "").strip().lower()
    return status == "ready"


def _nonblocking_record(target: dict[str, Any], status: str, reason: str) -> dict[str, Any]:
    record = dict(target)
    record.update(
        {
            "status": status,
            "native_live_action_available": False,
            "live_capture_guard_applicable": False,
            "nonblocking_scope": "native_lofty_listing_actions_only",
            "accounting_and_investor_reporting_included": True,
            "reason": reason,
        }
    )
    return record


def _normalize_name(value: Any) -> str:
    text = str(value or "").lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
