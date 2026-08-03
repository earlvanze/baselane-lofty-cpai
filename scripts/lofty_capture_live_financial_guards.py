#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lofty_index_status import is_active_index_status, is_excluded_index_status, normalize_index_status
from lofty_live_native_scope import (
    enrich_targets_from_active_roster,
    live_manager_mutation_ready,
    load_active_roster_scope,
    partition_current_manager_targets,
    validate_full_reporting_scope,
)
from lofty_monthly_exclusions import (
    DEFAULT_MANUAL_EXCLUDED_PROPERTIES,
    append_unmapped_exclusion_records,
    financial_hold_exclusion_records,
    guarded_apply_exclusion_records,
    match_exclusion_guard,
    monthly_exclusion_guards,
)
from lofty_property_paths import display_name_for_property_path, public_dir_for_property, resolve_index_property_path


GUARD_TIMEOUT_SECONDS = 30
LOFTY_CDP_RECOVERY_ACTION = (
    "Hard-refresh or close/open Lofty property-owners tab; authenticate only if still redirected, then rerun live FINANCIALS.md capture."
)
LOFTY_VISIBLE_AUTH_ACTION = "Auth Lofty visible tab (3 tries); then rerun live FINANCIALS.md capture."
SAFE_MONTHLY_CRON_DRY_RUN_COMMAND = (
    "DRY_RUN=1 CAPTURE_LOFTY_LIVE_GUARDS_IN_DRY_RUN=1 "
    "SEND_OWNER_EMAILS=0 PUBLISH_LOFTY_PM_UPDATES=0 "
    "RUN_LOFTY_GUARDED_APPLY=1 APPLY_LOFTY_GUARDED_UPDATES=0 bash scripts/baselane_financials_monthly_cron.sh"
)
DEFAULT_DISTRIBUTION_DISABLED_PROPERTIES = (
    "7542 and 7656 S Colfax Ave",
    "3139 West Blvd",
    "326-332 S Alcott St",
)
DEFAULT_CASH_SOURCE_GUARD_DISABLED_PROPERTIES = (
    "917 Pawnee Ave",
)
DEFAULT_NO_MORTGAGE_RESPONSIBILITY_PROPERTIES = (
    "86 Madison Ave",
    "88 Madison Ave",
    "90 Madison Ave",
    "724 3rd Ave",
    "85-104 Alawa Pl",
)
DEFAULT_NO_MORTGAGE_RESPONSIBILITY_STATES = ("IL", "OH", "TN")
DEFAULT_COO_OWNERSHIP_DISTRIBUTION_STATES = ("NY", "CA", "HI", "FL", "CO")
DEFAULT_COO_OWNERSHIP_ECO_CASH_MINIMUM = 3000.0
DEFAULT_LOFTY_TOKEN_PRICE = 50.0
PERCENT_READBACK_TOLERANCE = 0.01
DEFAULT_LISTING_UPDATE_POLICY = Path(__file__).resolve().parents[1] / "config" / "lofty_listing_update_policy.json"


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize(value: str) -> str:
    text = value.lower()
    text = re.sub(r"\blfty\d+\b", " ", text)
    text = text.replace("&", "and")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\bavenue\b", "ave", text)
    text = re.sub(r"\bstreet\b", "st", text)
    text = re.sub(r"\blane\b", "ln", text)
    text = re.sub(r"\bohio\b", "oh", text)
    return re.sub(r"\s+", " ", text).strip()


def property_id_from_href(value: str) -> str:
    match = re.search(r"/property-owners/edit/([A-Z0-9]+)", value or "")
    return match.group(1) if match else ""


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def listing_cash_flow_projection_override(
    policy: Any,
    property_name: str | None,
    run_month: str,
    *,
    policy_path: Path | None = None,
) -> dict[str, Any] | None:
    if not isinstance(policy, dict):
        return None
    target_key = normalize(property_name or "")
    if not target_key:
        return None
    for item in policy.get("projected_annual_cash_flow_overrides") or []:
        if not isinstance(item, dict):
            continue
        policy_name = str(item.get("address") or item.get("property_name") or "").strip()
        policy_key = normalize(policy_name)
        if not policy_key or not (
            target_key == policy_key or target_key in policy_key or policy_key in target_key
        ):
            continue
        exact_month = str(item.get("run_month") or "").strip()
        effective_from = str(item.get("effective_from") or "").strip()
        effective_through = str(item.get("effective_through") or "").strip()
        if exact_month and exact_month != run_month:
            continue
        if effective_from and run_month < effective_from:
            continue
        if effective_through and run_month > effective_through:
            continue
        amount = parse_live_number(item.get("projected_annual_cash_flow"))
        if amount is None:
            continue
        return {
            "projected_annual_cash_flow": round(max(amount, 0.0), 2),
            "property_name": policy_name,
            "run_month": exact_month or None,
            "effective_from": effective_from or None,
            "effective_through": effective_through or None,
            "reason": item.get("reason"),
            "evidence": item.get("evidence"),
            "approved_at": item.get("approved_at"),
            "policy_path": str(policy_path) if policy_path else None,
        }
    return None


def stable_digest(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def load_index(index_csv: Path) -> list[dict[str, str]]:
    with index_csv.open(newline="", encoding="utf-8") as handle:
        return [row for row in csv.DictReader(handle)]


def property_id_candidates(portfolio_map: Path | None, skill_map: Path | None) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    if portfolio_map and portfolio_map.is_file():
        data = load_json(portfolio_map)
        rows = data.get("properties") if isinstance(data, dict) else data
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            property_id = str(row.get("lofty_property_id") or row.get("property_id") or row.get("propertyId") or "").strip()
            if not property_id:
                property_id = property_id_from_href(str(row.get("editHref") or ""))
            for key_name in ("name", "full_address", "property_name", "slug"):
                key = str(row.get(key_name) or "").strip()
                if property_id and key:
                    candidates.append({"source": "portfolio_map", "key": key, "property_id": property_id, "normalized": normalize(key)})
    if skill_map and skill_map.is_file():
        data = load_json(skill_map)
        rows = data.get("properties") if isinstance(data, dict) else data
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            property_id = str(row.get("lofty_property_id") or "")
            for key_name in ("full_address", "property_name", "slug"):
                key = str(row.get(key_name) or "")
                if property_id and key:
                    candidates.append({"source": "skill_map", "key": key, "property_id": property_id, "normalized": normalize(key)})
    return candidates


def match_property_id(property_path: Path, candidates: list[dict[str, str]]) -> tuple[str | None, dict[str, Any]]:
    target = normalize(property_path.name)
    target_names = [target]
    if property_path.name.endswith(" Public"):
        stripped_target = normalize(property_path.name.removesuffix(" Public"))
        if stripped_target and stripped_target not in target_names:
            target_names.append(stripped_target)
    matches: list[tuple[int, dict[str, str]]] = []
    for candidate in candidates:
        key = candidate["normalized"]
        if not key:
            continue
        matched_target = next((item for item in target_names if key == item or key in item or item in key), "")
        if matched_target:
            score = len(key) + (1000 if key == matched_target else 0) + (100 if candidate["source"] == "portfolio_map" else 0)
            matches.append((score, candidate))
    matches.sort(key=lambda item: item[0], reverse=True)
    if not matches:
        return None, {"match_status": "unmatched", "normalized_property": target}
    top_score, top = matches[0]
    ambiguous = [candidate for score, candidate in matches if score == top_score and candidate["property_id"] != top["property_id"]]
    if ambiguous:
        return None, {"match_status": "ambiguous", "normalized_property": target, "candidates": [top, *ambiguous]}
    return top["property_id"], {"match_status": "matched", "match_source": top["source"], "match_key": top["key"]}


def externally_excluded_records(rows: list[dict[str, str]], exclusion_guards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in rows:
        if not is_active_index_status(row.get("status")):
            continue
        property_path, path_resolution = resolve_index_property_path(row)
        exclusion = match_exclusion_guard(property_path, exclusion_guards)
        if not exclusion:
            continue
        records.append(
            {
                "status": "excluded_no_live_update_or_email",
                "raw_status": str(row.get("status") or ""),
                "property_path": str(property_path),
                "property_name": property_path.name,
                "exclude_source": exclusion.get("source"),
                "exclude_reason": exclusion.get("exclude_reason"),
                "matched_exclusion_property": exclusion.get("property_name"),
                "yhome_column_b": exclusion.get("yhome_column_b"),
                **path_resolution,
            }
        )
    return records


def index_targets(
    rows: list[dict[str, str]],
    candidates: list[dict[str, str]],
    exclusion_guards: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for row in rows:
        if not is_active_index_status(row.get("status")):
            continue
        property_path, path_resolution = resolve_index_property_path(row)
        if match_exclusion_guard(property_path, exclusion_guards or []):
            continue
        public_dir = public_dir_for_property(property_path)
        financials_md = public_dir / "00 - README & Property Snapshot" / "FINANCIALS.md"
        property_id, match = match_property_id(property_path, candidates)
        property_name = display_name_for_property_path(property_path, path_resolution)
        targets.append(
            {
                "property_path": str(property_path),
                "property_name": property_name,
                "financials_md": str(financials_md),
                "lofty_property_id": property_id,
                **path_resolution,
                **match,
            }
        )
    return targets


def skipped_index_records(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in rows:
        if not is_excluded_index_status(row.get("status")):
            continue
        property_path, path_resolution = resolve_index_property_path(row)
        records.append(
            {
                "status": normalize_index_status(row.get("status")),
                "raw_status": str(row.get("status") or ""),
                "property_path": str(property_path),
                **path_resolution,
            }
        )
    return records


def extract_properties(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict) and isinstance(data.get("properties"), list):
        return data["properties"]
    if isinstance(payload, dict) and isinstance(payload.get("properties"), list):
        return payload["properties"]
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        return payload["items"]
    return []


def property_id_for_api_row(row: dict[str, Any]) -> str:
    for key in ("id", "propertyId", "property_id"):
        value = row.get(key)
        if value:
            return str(value)
    return ""


def fetch_manager_properties(skill_scripts_dir: Path, year: int, month: int, close_extra_tabs: bool) -> tuple[dict[str, Any] | None, str | None]:
    sys.path.insert(0, str(skill_scripts_dir))
    try:
        from update_lofty_pm_property import build_headers, capture_fresh, request  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return None, f"failed to import Lofty PM helpers: {exc}"
    try:
        from update_lofty_pm_property import request_get_manager_properties_via_turbopack_bridge  # type: ignore
    except Exception:  # noqa: BLE001
        request_get_manager_properties_via_turbopack_bridge = None  # type: ignore[assignment]
    payload = {"year": str(year), "month": str(month)}
    bridge_error: str | None = None
    if request_get_manager_properties_via_turbopack_bridge is not None:
        try:
            bridge = request_get_manager_properties_via_turbopack_bridge(
                payload,
                close_extra_tabs=close_extra_tabs,
            )
            data = bridge.get("response") if isinstance(bridge, dict) else None
            if isinstance(data, dict):
                return data, None
            bridge_error = f"Lofty PM Turbopack fetch returned {type(data).__name__}"
        except Exception as exc:  # noqa: BLE001
            bridge_error = f"Lofty PM Turbopack fetch failed: {exc}"
    try:
        headers = build_headers(capture_fresh("get-manager-properties", close_extra_tabs=close_extra_tabs, payload=payload))
        response = request("GET", "https://api.lofty.ai/prod/property-managers/v2/get-manager-properties", headers, payload)
    except Exception as exc:  # noqa: BLE001
        detail = f"Lofty PM API fetch failed: {exc}"
        return None, f"{detail}; {bridge_error}" if bridge_error else detail
    if not response.ok:
        detail = f"Lofty PM API fetch failed: HTTP {response.status_code} {response.text[:500]}"
        return None, f"{detail}; {bridge_error}" if bridge_error else detail
    return response.json(), None


def format_financials(skill_scripts_dir: Path, api_row: dict[str, Any], property_id: str) -> tuple[str | None, str | None]:
    sys.path.insert(0, str(skill_scripts_dir))
    try:
        from extract_lofty_property_data import format_financials_md  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return None, f"failed to import financial formatter: {exc}"
    try:
        return str(format_financials_md(api_row, property_id)).strip() + "\n", None
    except Exception as exc:  # noqa: BLE001
        return None, f"failed to format financials for {property_id}: {exc}"


def parse_money_cell(value: str) -> float | None:
    value = value.strip().replace(",", "")
    negative = value.startswith("-") or (value.startswith("(") and value.endswith(")"))
    number = re.search(r"\d+(?:\.\d+)?", value)
    if not number:
        return None
    amount = float(number.group(0))
    return -amount if negative else amount


def financials_table_value(text: str, label: str) -> float | None:
    match = re.search(rf"^\|\s*{re.escape(label)}\s*\|\s*([^|]+)\|", text, flags=re.IGNORECASE | re.MULTILINE)
    if not match:
        match = re.search(
            rf"^\s*[-*]?\s*{re.escape(label)}:\s*([^\n]+)$",
            text,
            flags=re.IGNORECASE | re.MULTILINE,
        )
    if not match:
        return None
    return parse_money_cell(match.group(1))


def open_accrual_requirement(text: str) -> float | None:
    table_value = financials_table_value(text, "Open accrual requirement")
    if table_value is not None:
        return table_value
    match = re.search(
        r"^\s*[-*]\s+Open accrual requirement:\s*\*{0,2}([^*\n]+)",
        text,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    return parse_money_cell(match.group(1)) if match else None


def coownership_state_for_path(financials_md: Path) -> str | None:
    parts = list(financials_md.parts)
    for idx, part in enumerate(parts[:-1]):
        if part == "Real Estate" and idx + 1 < len(parts):
            state = parts[idx + 1].strip().upper()
            if state in DEFAULT_COO_OWNERSHIP_DISTRIBUTION_STATES:
                return state
    return None


def no_mortgage_responsibility(property_name: str | None, financials_md: Path) -> bool:
    state = coownership_state_for_path(financials_md)
    if state in DEFAULT_NO_MORTGAGE_RESPONSIBILITY_STATES:
        return True
    names = [
        normalize(property_name or ""),
        normalize(financials_md.parent.parent.name if len(financials_md.parents) > 1 else ""),
        normalize(str(financials_md)),
    ]
    no_mortgage_names = [normalize(name) for name in DEFAULT_NO_MORTGAGE_RESPONSIBILITY_PROPERTIES]
    return any(
        name and no_mortgage and (name.startswith(no_mortgage) or no_mortgage.startswith(name) or no_mortgage in name)
        for name in names
        for no_mortgage in no_mortgage_names
    )


def distribution_is_manually_disabled(property_name: str | None, financials_md: Path) -> bool:
    names = [
        normalize(property_name or ""),
        normalize(financials_md.parent.parent.name if len(financials_md.parents) > 1 else ""),
        normalize(str(financials_md)),
    ]
    disabled_names = [normalize(name) for name in DEFAULT_DISTRIBUTION_DISABLED_PROPERTIES]
    return any(
        name and disabled and (name.startswith(disabled) or disabled.startswith(name) or disabled in name)
        for name in names
        for disabled in disabled_names
    )


def cash_source_guard_disabled(property_name: str | None, financials_md: Path) -> bool:
    names = [
        normalize(property_name or ""),
        normalize(financials_md.parent.parent.name if len(financials_md.parents) > 1 else ""),
        normalize(str(financials_md)),
    ]
    disabled_names = [normalize(name) for name in DEFAULT_CASH_SOURCE_GUARD_DISABLED_PROPERTIES]
    return any(
        name and disabled and (name.startswith(disabled) or disabled.startswith(name) or disabled in name)
        for name in names
        for disabled in disabled_names
    )


def combined_operating_cash_clearance(
    lofty_operating_cash: float | None,
    eco_operating_cash: float | None,
    maintenance_reserve: float = DEFAULT_COO_OWNERSHIP_ECO_CASH_MINIMUM,
) -> tuple[float | None, bool | None]:
    if lofty_operating_cash is None or eco_operating_cash is None:
        return None, None
    combined = round(lofty_operating_cash + eco_operating_cash, 2)
    return combined, combined > maintenance_reserve


def cash_source_distribution_guard_sources(financials_md: Path, property_name: str | None = None) -> list[str]:
    if cash_source_guard_disabled(property_name, financials_md):
        return []
    text = financials_md.read_text(encoding="utf-8", errors="ignore")
    lofty_cash = financials_table_value(text, "Lofty Operating Reserve")
    if lofty_cash is None:
        lofty_cash = financials_table_value(text, "Lofty maintenance reserve balance")
    if lofty_cash is None:
        lofty_cash = financials_table_value(text, "Cash held separately by Lofty")
    if lofty_cash is None:
        lofty_cash = financials_table_value(text, "Lofty Operating Cash")
    eco_cash = financials_table_value(text, "ECO Net DAO Funds (spendable cash held by ECO)")
    if eco_cash is None:
        eco_cash = financials_table_value(text, "Spendable cash ECO owes this DAO (ECO Net DAO Funds)")
    if eco_cash is None:
        eco_cash = financials_table_value(text, "ECO Operating Cash")
    dao_spendable_cash = financials_table_value(
        text,
        "Spendable Baselane/ECO cash after recorded obligations (before Lofty OR)",
    )
    if dao_spendable_cash is None:
        dao_spendable_cash = financials_table_value(
            text,
            "Spendable Baselane/ECO cash after recorded obligations",
        )
    eco_cash_source = "total_dao_spendable_cash"
    if dao_spendable_cash is None:
        physical_bank_cash = financials_table_value(text, "Cash in this DAO's own Baselane bank account")
        dao_spendable_cash = (
            round(eco_cash + physical_bank_cash, 2)
            if eco_cash is not None and physical_bank_cash is not None
            else eco_cash
        )
        eco_cash_source = "total_dao_spendable_cash" if physical_bank_cash is not None else "eco_held_unrestricted_cash"
    combined_cash, reserve_clear = combined_operating_cash_clearance(lofty_cash, dao_spendable_cash)
    if reserve_clear is True:
        return []
    sources: list[str] = []
    for label, source in (
        ("Lofty Operating Cash", "lofty_operating_cash"),
        ("Yhome Net Due to DAO", "yhome_net_due_to_dao"),
        ("YHome Net Due to DAO", "yhome_net_due_to_dao"),
    ):
        guard_value = financials_table_value(text, label)
        if guard_value is not None and guard_value <= 0 and source not in sources:
            sources.append(source)
    if dao_spendable_cash is not None and dao_spendable_cash <= 0:
        sources.append(eco_cash_source)
    if reserve_clear is False and "combined_operating_cash_below_maintenance_reserve" not in sources:
        sources.append("combined_operating_cash_below_maintenance_reserve")
    return sources


def expected_current_month_distribution(financials_md: Path, property_name: str | None = None) -> float | None:
    expected_annual = expected_projected_annual_cash_flow(financials_md, property_name)
    if expected_annual is None:
        return None
    return round(expected_annual / 12, 2)


def expected_live_cash_flow(financials_md: Path, property_name: str | None = None) -> float | None:
    return expected_projected_annual_cash_flow(financials_md, property_name)


def expected_projected_annual_cash_flow(financials_md: Path, property_name: str | None = None) -> float | None:
    text = financials_md.read_text(encoding="utf-8", errors="ignore")
    annual_basis = financials_table_value(text, "Projected Annual Cash Flow Basis")
    if annual_basis is not None:
        expected_annual = max(round(annual_basis, 2), 0.0)
    else:
        expected = financials_table_value(text, "Recurring Net Operating Cashflow")
        if expected is None:
            expected = financials_table_value(text, "Net Operating Cashflow")
            if expected is None:
                return None
            if (
                "scheduled_rent_run_rate_excess_cash_not_annualized" in text
                or "review_required_unattributed_multi_rent_cash_not_annualized" in text
            ):
                return None
        expected_annual = max(round(expected * 12, 2), 0.0)
    if distribution_is_manually_disabled(property_name, financials_md) or cash_source_distribution_guard_sources(financials_md, property_name):
        return 0.0
    return expected_annual


def cash_source_distribution_guard_active(financials_md: Path, property_name: str | None = None) -> bool:
    return bool(cash_source_distribution_guard_sources(financials_md, property_name))


def live_cashflow_per_unit_annual_cash_flow(live_row: dict[str, Any]) -> tuple[float | None, str | None]:
    rows = live_row.get("cashflow_per_unit")
    if not isinstance(rows, list):
        return None, None
    monthly_values: list[float] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = row.get("monthly_cash_flow")
        try:
            monthly_values.append(float(value))
        except (TypeError, ValueError):
            continue
    if not monthly_values:
        return None, None
    return round(sum(monthly_values) * 12, 2), "live_cashflow_per_unit_sum"


def parse_live_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def live_investment_denominator(live_row: dict[str, Any]) -> float | None:
    token_count = parse_live_number(live_row.get("currentTokenFloat"))
    if token_count is None or token_count <= 0:
        token_count = (
            parse_live_number(live_row.get("numIssued"))
            or parse_live_number(live_row.get("num_issued"))
            or parse_live_number(live_row.get("issuedTokens"))
            or parse_live_number(live_row.get("issued_tokens"))
        )
    if token_count is None or token_count <= 0:
        total_investment = parse_live_number(live_row.get("total_investment"))
        if total_investment is not None and total_investment > 0:
            return round(total_investment, 2)
    if token_count is None or token_count <= 0:
        token_count = (
            parse_live_number(live_row.get("tokens"))
            or parse_live_number(live_row.get("number_of_tokens"))
            or parse_live_number(live_row.get("numberOfTokens"))
        )
    if token_count is None or token_count <= 0:
        return None
    return round(token_count * DEFAULT_LOFTY_TOKEN_PRICE, 2)


def live_investment_denominator_source(live_row: dict[str, Any]) -> str | None:
    for key in ("currentTokenFloat", "numIssued", "num_issued", "issuedTokens", "issued_tokens"):
        value = parse_live_number(live_row.get(key))
        if value is not None and value > 0:
            return key if key == "currentTokenFloat" else f"{key}_currentTokenFloat"
    total_investment = parse_live_number(live_row.get("total_investment"))
    if total_investment is not None and total_investment > 0:
        return "total_investment"
    for key in ("tokens", "number_of_tokens", "numberOfTokens"):
        value = parse_live_number(live_row.get(key))
        if value is not None and value > 0:
            return key
    return None


def authoritative_floating_token_source(source: str | None) -> bool:
    if not source:
        return False
    return source in {
        "currentTokenFloat",
        "numIssued_currentTokenFloat",
        "num_issued_currentTokenFloat",
        "issuedTokens_currentTokenFloat",
        "issued_tokens_currentTokenFloat",
        "verifiedFloatingEquityTokens",
        "verified_floating_equity_tokens",
        "onchainFloatingEquityTokens",
        "onchain_floating_equity_tokens",
        "floatingEquityTokens",
        "floating_equity_tokens",
    }


def expected_percent_of_live_investment(amount: float | None, live_row: dict[str, Any]) -> float | None:
    if amount is None:
        return None
    if amount <= 0:
        return 0.0
    denominator = live_investment_denominator(live_row)
    if denominator is None or denominator <= 0:
        return None
    return round((amount / denominator) * 100, 2)


def percent_readback_ok(actual: float | None, expected: float | None) -> bool:
    if expected is None or actual is None:
        return True
    return abs(actual - expected) <= PERCENT_READBACK_TOLERANCE + 1e-9


def verify_live_distribution(
    financials_md: Path,
    live_row: dict[str, Any],
    property_name: str | None = None,
    projection_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    local_expected = expected_live_cash_flow(financials_md, property_name)
    local_expected_current_month_distribution_value = expected_current_month_distribution(financials_md, property_name)
    live_unit_expected, live_unit_expected_source = live_cashflow_per_unit_annual_cash_flow(live_row)
    use_live_unit_expected = False
    override_amount = parse_live_number(
        projection_override.get("projected_annual_cash_flow")
        if isinstance(projection_override, dict)
        else None
    )
    expected = round(max(override_amount, 0.0), 2) if override_amount is not None else local_expected
    expected_source = (
        "listing_update_policy_override"
        if override_amount is not None
        else "local_financials_md"
    )
    expected_current_month_distribution_value = (
        round(expected / 12, 2) if expected is not None else local_expected_current_month_distribution_value
    )
    actual = live_row.get("cash_flow")
    if expected is None:
        return {"targeted": False, "ok": True, "expected": None, "actual": actual}
    try:
        actual_number = float(actual)
    except (TypeError, ValueError):
        return {"targeted": True, "ok": False, "expected": expected, "actual": actual}
    guard_sources = cash_source_distribution_guard_sources(financials_md, property_name)
    guard_active = bool(guard_sources)
    manual_disable = distribution_is_manually_disabled(property_name, financials_md)
    coownership_state = coownership_state_for_path(financials_md)
    denominator_source = live_investment_denominator_source(live_row)
    missing_authoritative_floating_tokens = bool(
        coownership_state
        and expected > 0
        and not authoritative_floating_token_source(denominator_source)
    )
    if guard_active or manual_disable or missing_authoritative_floating_tokens:
        expected = 0.0
        expected_current_month_distribution_value = 0.0
    coc_actual = live_row.get("coc")
    try:
        coc_actual_number = float(coc_actual)
    except (TypeError, ValueError):
        coc_actual_number = None
    yield_actual = live_row.get("projected_rental_yield")
    try:
        yield_actual_number = float(yield_actual)
    except (TypeError, ValueError):
        yield_actual_number = None
    is_occupied_actual = live_row.get("is_occupied")
    cash_flow_ok = abs(expected - actual_number) < 0.005
    expected_annual_cash_flow = expected
    annual_cash_flow_disabled = expected_annual_cash_flow is not None and expected_annual_cash_flow <= 0
    distribution_disabled = guard_active or annual_cash_flow_disabled or manual_disable or missing_authoritative_floating_tokens
    expected_return_percent = expected_percent_of_live_investment(expected_annual_cash_flow, live_row)
    if distribution_disabled:
        expected_coc = 0.0
        expected_yield = 0.0
        expected_occupied = False
    else:
        expected_coc = expected_return_percent
        expected_yield = expected_return_percent
        expected_occupied = bool(expected_annual_cash_flow and expected_annual_cash_flow > 0)
    no_mortgage = no_mortgage_responsibility(property_name, financials_md)
    current_loan_actual = parse_live_number(live_row.get("current_loan"))
    monthly_loan_repayment_actual = parse_live_number(live_row.get("monthly_loan_repayment"))
    current_loan_ok = True if not no_mortgage else bool(current_loan_actual is None or abs(current_loan_actual) < 0.005)
    no_mortgage_downstream_ok = (
        True
        if not no_mortgage
        else bool(monthly_loan_repayment_actual is None or abs(monthly_loan_repayment_actual) < 0.005)
    )
    current_loan_warning = bool(no_mortgage and not current_loan_ok and no_mortgage_downstream_ok)
    coc_ok = percent_readback_ok(coc_actual_number, expected_coc)
    yield_ok = percent_readback_ok(yield_actual_number, expected_yield)
    occupancy_ok = is_occupied_actual is expected_occupied
    return {
        "targeted": True,
        "ok": cash_flow_ok and coc_ok and yield_ok and occupancy_ok and no_mortgage_downstream_ok,
        "expected": expected,
        "expected_source": expected_source,
        "expected_current_month_distribution": expected_current_month_distribution_value,
        "local_expected": local_expected,
        "local_expected_current_month_distribution": local_expected_current_month_distribution_value,
        "listing_cash_flow_projection_override": projection_override,
        "live_cashflow_per_unit_annual_cash_flow": live_unit_expected,
        "live_cashflow_per_unit_annual_cash_flow_source": live_unit_expected_source,
        "live_cashflow_per_unit_used": use_live_unit_expected,
        "cash_flow_api_semantics": "annualized; Lofty UI Current Month Distribution is cash_flow / 12",
        "actual": actual_number,
        "cash_flow_ok": cash_flow_ok,
        "cash_source_guard_active": guard_active,
        "cash_source_guard_sources": guard_sources,
        "manual_distribution_disable": manual_disable,
        "coownership_distribution_state": coownership_state,
        "coownership_eco_cash_minimum": DEFAULT_COO_OWNERSHIP_ECO_CASH_MINIMUM
        if coownership_state
        else None,
        "missing_authoritative_floating_equity_tokens": missing_authoritative_floating_tokens,
        "cash_on_cash_denominator_source_authoritative": authoritative_floating_token_source(denominator_source),
        "annual_cash_flow_disabled": annual_cash_flow_disabled,
        "expected_projected_annual_cash_flow": expected_annual_cash_flow,
        "distribution_disabled": distribution_disabled,
        "expected_coc": expected_coc,
        "actual_coc": coc_actual_number,
        "coc_ok": coc_ok,
        "expected_projected_rental_yield": expected_yield,
        "actual_projected_rental_yield": yield_actual_number,
        "projected_rental_yield_ok": yield_ok,
        "expected_is_occupied": expected_occupied,
        "actual_is_occupied": is_occupied_actual,
        "is_occupied_ok": occupancy_ok,
        "no_mortgage_responsibility": no_mortgage,
        "expected_current_loan": 0.0 if no_mortgage else None,
        "actual_current_loan": current_loan_actual,
        "current_loan_ok": current_loan_ok,
        "current_loan_warning": current_loan_warning,
        "current_loan_warning_reason": (
            "Lofty property-manager update-manager-property accepts current_loan=0 but readback restores the backend-derived value; monthly_loan_repayment is zero so downstream distribution math is not affected."
            if current_loan_warning
            else None
        ),
        "expected_monthly_loan_repayment": 0.0 if no_mortgage else None,
        "actual_monthly_loan_repayment": monthly_loan_repayment_actual,
        "monthly_loan_repayment_ok": no_mortgage_downstream_ok,
        "cash_on_cash_denominator": live_investment_denominator(live_row),
        "cash_on_cash_denominator_source": denominator_source,
        "percent_readback_tolerance": PERCENT_READBACK_TOLERANCE,
    }


def run_guard(command: list[str]) -> dict[str, Any]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=GUARD_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "return_code": 124,
            "ok": False,
            "stdout_tail": (exc.stdout or "")[-2000:] if isinstance(exc.stdout, str) else "",
            "stderr_tail": f"guard command timed out after {GUARD_TIMEOUT_SECONDS}s",
            "timed_out": True,
        }
    return {
        "command": command,
        "return_code": result.returncode,
        "ok": result.returncode == 0,
        "stdout_tail": result.stdout[-2000:],
        "stderr_tail": result.stderr[-2000:],
    }


def shell_command(parts: list[object]) -> str:
    return " ".join(shlex.quote(str(part)) for part in parts if str(part or "").strip())


def safe_monthly_cron_dry_run_command() -> str:
    env_root = os.environ.get("OPENCLAW_WORKSPACE") or os.environ.get("WORKSPACE_ROOT")
    if env_root:
        return f"cd {shlex.quote(str(Path(env_root)))} && {SAFE_MONTHLY_CRON_DRY_RUN_COMMAND}"
    cwd = Path.cwd()
    root = cwd if (cwd / "scripts" / "baselane_financials_monthly_cron.sh").is_file() else Path(__file__).absolute().parents[1]
    return f"cd {shlex.quote(str(root))} && {SAFE_MONTHLY_CRON_DRY_RUN_COMMAND}"


def capture_rerun_command(args: argparse.Namespace, *, apply: bool) -> str:
    parts: list[object] = [
        "python3",
        Path("scripts") / Path(__file__).name,
        "--index-csv",
        args.index_csv,
        "--report",
        args.report,
        "--live-guard",
        args.live_guard,
        "--skill-scripts-dir",
        args.skill_scripts_dir,
        "--artifact-dir",
        args.artifact_dir,
        "--year",
        args.year,
        "--month",
        args.month,
    ]
    if args.portfolio_map:
        parts.extend(["--portfolio-map", args.portfolio_map])
    if args.skill_map:
        parts.extend(["--skill-map", args.skill_map])
    if args.active_roster_report:
        parts.extend(["--active-roster-report", args.active_roster_report])
    if args.max_properties:
        parts.extend(["--max-properties", args.max_properties])
    if args.yhome_transition_csv:
        parts.extend(["--yhome-transition-csv", args.yhome_transition_csv])
    if args.transfer_reconciliation_report:
        parts.extend(["--transfer-reconciliation-report", args.transfer_reconciliation_report])
    if args.guarded_apply_report:
        parts.extend(["--guarded-apply-report", args.guarded_apply_report])
    for property_name in args.manual_excluded_property or []:
        parts.extend(["--manual-excluded-property", property_name])
    if apply:
        parts.append("--apply")
    if args.bootstrap_missing:
        parts.append("--bootstrap-missing")
    if args.close_extra_tabs:
        parts.append("--close-extra-tabs")
    return shell_command(parts)


def is_lofty_auth_issue(issue: str) -> bool:
    text = str(issue or "").lower()
    return (
        "lofty pm api fetch failed" in text
        and (
            "unauthorized" in text
            or "http 401" in text
            or '"code":401' in text
            or '"httpcode":401' in text
            or "'code': 401" in text
            or "'httpcode': 401" in text
        )
    )


def is_lofty_capture_transport_issue(issue: str) -> bool:
    text = str(issue or "").lower()
    return "lofty pm api fetch failed" in text and (
        "fresh auth capture failed" in text
        or "did not capture a signed lofty api request" in text
        or "turbopack runtime not available" in text
        or "turbopack bridge failed" in text
    )


def lofty_preflight_recovery_exhausted(report_path: Path) -> bool:
    preflight_path = report_path.parent / "lofty_cdp_preflight_report.json"
    if not preflight_path.is_file():
        return False
    try:
        preflight = load_json(preflight_path)
    except Exception:
        return False
    if not isinstance(preflight, dict):
        return False
    return bool(
        preflight.get("automated_browser_recovery_complete")
        or preflight.get("login_recovery_exhausted")
        or preflight.get("manual_auth_phase") == "after_browser_recovery"
    )


def lofty_cdp_recovery_action(args: argparse.Namespace) -> str:
    if lofty_preflight_recovery_exhausted(args.report):
        return LOFTY_VISIBLE_AUTH_ACTION
    return LOFTY_CDP_RECOVERY_ACTION


def report_next_action(status: str, args: argparse.Namespace, issues: list[str], capture_ready: bool) -> dict[str, Any]:
    rerun_command = safe_monthly_cron_dry_run_command()
    recovery_action = lofty_cdp_recovery_action(args)
    if capture_ready:
        return {
            "status": "ready",
            "summary": "Live Lofty FINANCIALS.md guard capture is current for all manager-actionable reporting targets.",
            "rerun_command": rerun_command,
            "requires_authenticated_cdp": False,
            "holds_live_publish_and_owner_email": False,
        }
    if issues:
        if is_lofty_auth_issue(issues[0]):
            return {
                "status": "fix_capture_prerequisite",
                "summary": recovery_action,
                "diagnostic": issues[0],
                "auth_issue_class": "lofty_pm_unauthorized",
                "rerun_command": rerun_command,
                "requires_authenticated_cdp": True,
                "holds_live_publish_and_owner_email": True,
            }
        if is_lofty_capture_transport_issue(issues[0]):
            return {
                "status": "fix_capture_prerequisite",
                "summary": recovery_action,
                "diagnostic": issues[0],
                "capture_issue_class": "lofty_pm_capture_transport_unavailable",
                "rerun_command": rerun_command,
                "requires_authenticated_cdp": True,
                "holds_live_publish_and_owner_email": True,
            }
        return {
            "status": "fix_capture_prerequisite",
            "summary": issues[0],
            "rerun_command": rerun_command,
            "requires_authenticated_cdp": bool(args.apply),
            "holds_live_publish_and_owner_email": True,
        }
    if not args.apply:
        return {
            "status": "capture_authenticated_live_financials",
            "summary": recovery_action,
            "rerun_command": rerun_command,
            "requires_authenticated_cdp": True,
            "holds_live_publish_and_owner_email": True,
        }
    return {
        "status": "reconcile_live_financial_guards" if status == "review" else "review_live_financial_capture_failure",
        "summary": "Use records[].next_action_command for each unverified FINANCIALS.md target, then rerun capture.",
        "rerun_command": rerun_command,
        "requires_authenticated_cdp": True,
        "holds_live_publish_and_owner_email": True,
    }


def status_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        status = str(record.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def review_blockers(
    *,
    apply: bool,
    planned_count: int,
    blocked_count: int,
    mismatch_count: int,
    unverified_count: int,
) -> list[str]:
    blockers: list[str] = []
    if not apply:
        blockers.append("live_financial_capture_not_applied")
    if planned_count:
        blockers.append(f"live_financial_planned_count={planned_count}")
    if blocked_count:
        blockers.append(f"live_financial_blocked_count={blocked_count}")
    if mismatch_count:
        blockers.append(f"live_financial_mismatch_count={mismatch_count}")
    if unverified_count:
        blockers.append(f"live_financial_unverified_count={unverified_count}")
    return blockers


def add_next_action(record: dict[str, Any], live_guard: Path) -> None:
    status = str(record.get("status") or "")
    financials_md = record.get("financials_md")
    snapshot_path = record.get("snapshot_path")
    if status == "blocked_no_property_id":
        record.update(
            {
                "next_action_stage": "map_lofty_property_id",
                "next_action_file": record.get("property_path") or "",
                "next_action_command": "",
                "next_action_detail": "Add or fix this property's Lofty property id in the portfolio/skill map before live financial capture.",
            }
        )
        return
    if status == "blocked_missing_financials_md":
        record.update(
            {
                "next_action_stage": "restore_financials_md",
                "next_action_file": financials_md or "",
                "next_action_command": "",
                "next_action_detail": "Restore or bootstrap canonical Public/00 - README & Property Snapshot/FINANCIALS.md before live guard apply.",
            }
        )
        return
    if status in {"planned", "blocked_missing_live_api_row", "blocked_format_failed", "needs_reconcile"}:
        record.update(
            {
                "next_action_stage": "capture_financial_live_guard",
                "next_action_file": snapshot_path or financials_md or "",
                "next_action_command": shell_command(
                    [
                        live_guard,
                        "capture-fetch",
                        financials_md,
                        snapshot_path,
                        "--source",
                        "Lofty PM get-manager-properties financial data",
                    ]
                ),
                "next_action_detail": "Fetch/format live Lofty financial data, register it with the live-file guard, then run the FINANCIALS.md guard check.",
            }
        )
        return
    if status in {"guard_ok", "guard_ok_live_distribution", "guard_ok_no_distribution_target"}:
        record.update(
            {
                "next_action_stage": "",
                "next_action_file": "",
                "next_action_command": "",
                "next_action_detail": "Live FINANCIALS.md guard verified.",
            }
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture live Lofty PM financial data and register FINANCIALS.md live-file guard artifacts.")
    parser.add_argument("--index-csv", required=True, type=Path)
    parser.add_argument("--active-roster-report", type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--portfolio-map", type=Path)
    parser.add_argument("--skill-map", type=Path)
    parser.add_argument("--live-guard", required=True, type=Path)
    parser.add_argument("--skill-scripts-dir", required=True, type=Path)
    parser.add_argument("--artifact-dir", required=True, type=Path)
    parser.add_argument("--year", type=int, default=datetime.now().year)
    parser.add_argument("--month", type=int, default=datetime.now().month)
    parser.add_argument("--max-properties", type=int, default=0)
    parser.add_argument("--yhome-transition-csv", type=Path)
    parser.add_argument("--manual-excluded-property", action="append", default=[])
    parser.add_argument("--transfer-reconciliation-report", type=Path)
    parser.add_argument("--guarded-apply-report", type=Path)
    parser.add_argument("--listing-update-policy", type=Path, default=DEFAULT_LISTING_UPDATE_POLICY)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--bootstrap-missing", action="store_true")
    parser.add_argument("--close-extra-tabs", action="store_true")
    args = parser.parse_args()
    run_month = f"{args.year:04d}-{args.month:02d}"

    issues: list[str] = []
    listing_update_policy: dict[str, Any] = {}
    if not args.index_csv.is_file():
        issues.append(f"monthly index missing: {args.index_csv}")
    if not args.live_guard.is_file():
        issues.append(f"live guard missing: {args.live_guard}")
    if not (args.skill_scripts_dir / "update_lofty_pm_property.py").is_file():
        issues.append(f"Lofty PM helper missing: {args.skill_scripts_dir / 'update_lofty_pm_property.py'}")
    if not (args.skill_scripts_dir / "extract_lofty_property_data.py").is_file():
        issues.append(f"Lofty PM financial formatter missing: {args.skill_scripts_dir / 'extract_lofty_property_data.py'}")
    if not args.listing_update_policy.is_file():
        issues.append(f"Lofty listing update policy missing: {args.listing_update_policy}")
    else:
        try:
            policy_payload = load_json(args.listing_update_policy)
            if isinstance(policy_payload, dict):
                listing_update_policy = policy_payload
            else:
                issues.append(f"Lofty listing update policy must be a JSON object: {args.listing_update_policy}")
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(f"Lofty listing update policy unreadable: {args.listing_update_policy}: {exc}")

    if issues and args.apply:
        report = {
            "generated_at": iso_z(),
            "status": "failed",
            "apply": args.apply,
            "live_capture": args.apply,
            "mutates_lofty_listing": False,
            "mutates_external_system": False,
            "external_mutation_count": 0,
            "capture_semantics": "authenticated_read_and_guard_registration_only",
            "sends_owner_email": False,
            "issues": issues,
            "issue_count": len(issues),
            "target_count": 0,
            "records": [],
            "next_action": {
                "status": "fix_capture_prerequisite",
                "summary": issues[0],
                "rerun_command": safe_monthly_cron_dry_run_command(),
                "requires_authenticated_cdp": True,
                "holds_live_publish_and_owner_email": True,
            },
            "holds_live_publish_and_owner_email": True,
        }
        diagnostic_path = args.report.with_suffix(args.report.suffix + ".prerequisite_failed.json")
        diagnostic_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({key: report[key] for key in ("status", "issue_count", "target_count")}, indent=2, sort_keys=True))
        return 1

    rows = load_index(args.index_csv) if args.index_csv.is_file() else []
    targeted_run = args.max_properties > 0
    active_roster_scope = load_active_roster_scope(args.active_roster_report)
    authoritative_roster_scope = (
        not targeted_run
        and active_roster_scope.get("status") == "ok"
        and bool(active_roster_scope.get("records"))
    )
    skipped_records = skipped_index_records(rows)
    manual_names = [*DEFAULT_MANUAL_EXCLUDED_PROPERTIES, *args.manual_excluded_property]
    exclusion_guards, yhome_guard, manual_exclusions = monthly_exclusion_guards(
        args.yhome_transition_csv,
        manual_names,
    )
    target_exclusion_guards, _, _ = monthly_exclusion_guards(args.yhome_transition_csv, manual_names)
    financial_hold_exclusions = financial_hold_exclusion_records(args.transfer_reconciliation_report)
    guarded_apply_exclusions = guarded_apply_exclusion_records(args.guarded_apply_report)
    exclusion_guards.extend(financial_hold_exclusions)
    target_exclusion_guards.extend([*financial_hold_exclusions, *guarded_apply_exclusions])
    external_exclusion_candidates = externally_excluded_records(rows, exclusion_guards)
    if not authoritative_roster_scope:
        append_unmapped_exclusion_records(
            external_exclusion_candidates,
            guarded_apply_exclusions,
            represented_records=skipped_records,
        )
    external_excluded_records = [] if authoritative_roster_scope else external_exclusion_candidates
    candidates = property_id_candidates(args.portfolio_map, args.skill_map)
    reporting_targets = index_targets(
        rows,
        candidates,
        [] if authoritative_roster_scope else target_exclusion_guards,
    )
    if args.max_properties > 0:
        reporting_targets = reporting_targets[: args.max_properties]
    reporting_targets, roster_unmatched_records = enrich_targets_from_active_roster(
        reporting_targets,
        active_roster_scope,
    )
    if authoritative_roster_scope and roster_unmatched_records:
        issues.append(
            "active roster failed to match reporting targets: "
            + ", ".join(str(record.get("property_name") or record.get("property_path") or "unknown") for record in roster_unmatched_records)
        )
    issues.extend(
        validate_full_reporting_scope(
            active_roster_scope,
            len(reporting_targets),
            targeted=targeted_run,
        )
    )
    portfolio_reporting_target_count = (
        active_roster_scope.get("portfolio_reporting_target_count") or len(reporting_targets)
    )

    live_by_id: dict[str, dict[str, Any]] = {}
    if args.apply and not issues:
        api_payload, api_error = fetch_manager_properties(args.skill_scripts_dir, args.year, args.month, args.close_extra_tabs)
        if api_error:
            issues.append(api_error)
        else:
            for api_row in extract_properties(api_payload or {}):
                property_id = property_id_for_api_row(api_row)
                if property_id:
                    live_by_id[property_id] = api_row
    partition = partition_current_manager_targets(
        reporting_targets,
        live_property_ids=set(live_by_id) if args.apply and not api_error and not issues else None,
        mutation_ready_property_ids=(
            {property_id for property_id, row in live_by_id.items() if live_manager_mutation_ready(row)}
            if args.apply and not api_error and not issues
            else None
        ),
    )
    targets = partition["captureable"]
    mutation_ready_targets = partition["actionable"]
    known_id_targets = partition["known_id"]
    manager_unavailable_records = partition["manager_unavailable"]
    mutation_unavailable_targets = partition["mutation_unavailable"]
    native_unavailable_records = partition["no_id"]
    live_scope_source = (
        "authenticated_get_manager_properties"
        if args.apply and not api_error and not issues
        else "active_property_roster"
    )

    records: list[dict[str, Any]] = []
    bootstrap_count = 0
    register_count = 0
    check_ok_count = 0
    mismatch_count = 0
    sparse_snapshot_diff_count = 0
    for target in targets:
        record = dict(target)
        property_id = target.get("lofty_property_id")
        financials_md = Path(target["financials_md"])
        snapshot_path = args.artifact_dir / property_id / "live-FINANCIALS.md"
        record["snapshot_path"] = str(snapshot_path)
        if record.get("live_capture_guard_applicable") is False:
            record["status"] = "guard_not_applicable_mutation_unavailable"
            record["target_exists"] = financials_md.is_file()
            record["nonblocking_scope"] = "native_lofty_listing_actions_only"
            record["accounting_and_investor_reporting_included"] = True
            records.append(record)
            continue
        if not financials_md.is_file():
            record["status"] = "blocked_missing_financials_md"
            record["target_exists"] = False
            record["bootstrap_missing_financials_md_disabled"] = bool(args.bootstrap_missing)
            add_next_action(record, args.live_guard)
            records.append(record)
            continue
        if not args.apply or issues:
            record["status"] = "planned"
            record["target_exists"] = financials_md.is_file()
            add_next_action(record, args.live_guard)
            records.append(record)
            continue
        live_row = live_by_id.get(str(property_id))
        if not live_row:
            record["status"] = "blocked_missing_live_api_row"
            add_next_action(record, args.live_guard)
            records.append(record)
            continue
        financials_text, format_error = format_financials(args.skill_scripts_dir, live_row, str(property_id))
        if format_error or financials_text is None:
            record["status"] = "blocked_format_failed"
            record["error"] = format_error
            add_next_action(record, args.live_guard)
            records.append(record)
            continue
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(financials_text, encoding="utf-8")
        record["live_financials_length"] = len(financials_text)
        register = run_guard(
            [
                sys.executable,
                str(args.live_guard),
                "capture-fetch",
                str(financials_md),
                str(snapshot_path),
                "--source",
                "Lofty PM get-manager-properties financial data",
            ]
        )
        record["register"] = register
        if register["ok"]:
            register_count += 1
        check = run_guard([sys.executable, str(args.live_guard), "check", str(financials_md)])
        record["check"] = check
        projection_override = listing_cash_flow_projection_override(
            listing_update_policy,
            str(target.get("property_name") or ""),
            run_month,
            policy_path=args.listing_update_policy,
        )
        record["listing_cash_flow_projection_override"] = projection_override
        distribution_verify = verify_live_distribution(
            financials_md,
            live_row,
            str(target.get("property_name") or ""),
            projection_override,
        )
        record["live_distribution_verify"] = distribution_verify
        if check["ok"]:
            check_ok_count += 1
            record["status"] = "guard_ok"
        elif distribution_verify["ok"]:
            sparse_snapshot_diff_count += 1
            check_ok_count += 1
            record["status"] = (
                "guard_ok_live_distribution"
                if distribution_verify["targeted"]
                else "guard_ok_no_distribution_target"
            )
        else:
            mismatch_count += 1
            record["status"] = "blocked_live_distribution_mismatch"
        add_next_action(record, args.live_guard)
        records.append(record)

    planned_count = sum(1 for record in records if record.get("status") == "planned")
    blocked_count = sum(1 for record in records if str(record.get("status", "")).startswith("blocked_"))
    capture_ready_count = sum(
        1
        for record in records
        if (
            isinstance(record.get("register"), dict)
            and record["register"].get("ok") is True
            and str(record.get("status") or "").startswith("guard_ok")
        )
        or record.get("status") == "guard_not_applicable_mutation_unavailable"
    )
    local_reconcile_required_count = sparse_snapshot_diff_count
    unverified_count = max(0, len(targets) - capture_ready_count)
    all_records = [*records, *manager_unavailable_records, *native_unavailable_records]
    target_digest = stable_digest(
        {
            "records": [
                {
                    "property_name": record.get("property_name"),
                    "property_path": record.get("property_path"),
                    "financials_md": record.get("financials_md"),
                    "lofty_property_id": record.get("lofty_property_id"),
                    "status": record.get("status"),
                    "snapshot_path": record.get("snapshot_path"),
                    "target_exists": record.get("target_exists"),
                    "next_action_stage": record.get("next_action_stage"),
                    "next_action_file": record.get("next_action_file"),
                    "next_action_command": record.get("next_action_command"),
                }
                for record in all_records
            ]
        }
    )
    capture_ready = (
        bool(args.apply)
        and not issues
        and not planned_count
        and not blocked_count
        and register_count == len(mutation_ready_targets)
        and check_ok_count == len(mutation_ready_targets)
        and capture_ready_count == len(targets)
    )
    status = "failed" if issues and args.apply else "ok" if capture_ready else "review"
    next_action = report_next_action(status, args, issues, capture_ready)
    review_blocker_list = (
        []
        if status == "ok"
        else review_blockers(
            apply=args.apply,
            planned_count=planned_count,
            blocked_count=blocked_count,
            mismatch_count=mismatch_count,
            unverified_count=unverified_count,
        )
    )
    record_status_counts = status_counts(all_records)
    report = {
        "generated_at": iso_z(),
        "status": status,
        "apply": args.apply,
        "live_capture": args.apply,
        "mutates_lofty_listing": False,
        "mutates_external_system": False,
        "external_mutation_count": 0,
        "capture_semantics": "authenticated_read_and_guard_registration_only",
        "sends_owner_email": False,
        "bootstrap_missing": args.bootstrap_missing,
        "year": args.year,
        "month": args.month,
        "issues": issues,
        "issue_count": len(issues),
        "review_blockers": review_blocker_list,
        "review_blocker_count": len(review_blocker_list),
        "review_blocker_summary": review_blocker_list[0] if review_blocker_list else None,
        "next_action": next_action,
        "rerun_command": next_action["rerun_command"],
        "requires_authenticated_cdp": next_action["requires_authenticated_cdp"],
        "holds_live_publish_and_owner_email": next_action["holds_live_publish_and_owner_email"],
        "physical_property_count": active_roster_scope.get("physical_property_count"),
        "portfolio_reporting_target_count": portfolio_reporting_target_count,
        "selected_reporting_target_count": len(reporting_targets),
        "target_count": len(reporting_targets),
        "target_count_semantics": "monthly_reporting_targets",
        "capture_target_count": len(targets),
        "native_live_target_count": len(targets),
        "known_lofty_property_id_count": len(known_id_targets),
        "current_manager_live_target_count": len(targets),
        "current_manager_mutation_ready_count": len(mutation_ready_targets),
        "current_manager_mutation_unavailable_count": len(mutation_unavailable_targets),
        "current_manager_unavailable_count": len(manager_unavailable_records),
        "native_unavailable_count": len(native_unavailable_records),
        "live_action_unavailable_count": (
            len(mutation_unavailable_targets) + len(manager_unavailable_records) + len(native_unavailable_records)
        ),
        "current_manager_scope_source": live_scope_source,
        "listing_update_policy": str(args.listing_update_policy),
        "current_manager_mutation_unavailable_records": mutation_unavailable_targets,
        "current_manager_unavailable_records": manager_unavailable_records,
        "native_unavailable_records": native_unavailable_records,
        "active_roster_scope": {key: value for key, value in active_roster_scope.items() if key != "records"},
        "active_roster_record_count": len(active_roster_scope.get("records") or []),
        "authoritative_roster_scope_applied": authoritative_roster_scope,
        "legacy_exclusion_scope_reduction_applied": not authoritative_roster_scope,
        "external_exclusion_candidate_count": len(external_exclusion_candidates),
        "external_exclusion_candidates": external_exclusion_candidates,
        "active_roster_unmatched_count": len(roster_unmatched_records),
        "active_roster_unmatched_records": roster_unmatched_records,
        "skipped_index_count": len(skipped_records),
        "skipped_index_status_counts": {
            status: sum(1 for record in skipped_records if record.get("status") == status)
            for status in sorted({str(record.get("status") or "") for record in skipped_records})
        },
        "skipped_index_digest": stable_digest({"records": skipped_records}),
        "skipped_index_records": skipped_records,
        "externally_excluded_count": len(external_excluded_records),
        "externally_excluded_records": external_excluded_records,
        "excluded_property_count": len(skipped_records) + len(external_excluded_records),
        "excluded_property_names": [
            *[Path(str(record.get("property_path") or "")).name for record in skipped_records],
            *[str(record.get("property_name") or "") for record in external_excluded_records],
        ],
        "yhome_transition_guard": yhome_guard,
        "manual_excluded_property_names": [record["property_name"] for record in manual_exclusions],
        "planned_count": planned_count,
        "blocked_count": blocked_count,
        "bootstrap_count": bootstrap_count,
        "register_count": register_count,
        "required_register_count": len(mutation_ready_targets),
        "guard_check_ok_count": check_ok_count,
        "check_ok_count": check_ok_count,
        "required_check_ok_count": len(mutation_ready_targets),
        "unverified_count": unverified_count,
        "capture_ready_count": capture_ready_count,
        "capture_missing_count": unverified_count,
        "local_reconcile_required_count": local_reconcile_required_count,
        "mismatch_count": mismatch_count,
        "sparse_snapshot_diff_count": sparse_snapshot_diff_count,
        "check_ok_semantics": "authenticated live cash_flow readback equals the guarded monthly Current Month Distribution target, and cash-source-guarded rows also require coc=0, projected_rental_yield=0, and is_occupied=false; full FINANCIALS.md may intentionally differ from sparse Lofty fields",
        "record_status_counts": record_status_counts,
        "target_digest": target_digest,
        "capture_contract": {
            "ready": capture_ready,
            "apply": args.apply,
            "live_capture": args.apply,
            "mutates_lofty_listing": False,
            "mutates_external_system": False,
            "external_mutation_count": 0,
            "capture_semantics": "authenticated_read_and_guard_registration_only",
            "sends_owner_email": False,
            "physical_property_count": active_roster_scope.get("physical_property_count"),
            "portfolio_reporting_target_count": portfolio_reporting_target_count,
            "selected_reporting_target_count": len(reporting_targets),
            "target_count": len(reporting_targets),
            "target_count_semantics": "monthly_reporting_targets",
            "capture_target_count": len(targets),
            "native_live_target_count": len(targets),
            "known_lofty_property_id_count": len(known_id_targets),
            "current_manager_live_target_count": len(targets),
            "current_manager_mutation_ready_count": len(mutation_ready_targets),
            "current_manager_mutation_unavailable_count": len(mutation_unavailable_targets),
            "current_manager_unavailable_count": len(manager_unavailable_records),
            "native_unavailable_count": len(native_unavailable_records),
            "live_action_unavailable_count": (
                len(mutation_unavailable_targets) + len(manager_unavailable_records) + len(native_unavailable_records)
            ),
            "current_manager_scope_source": live_scope_source,
            "bootstrap_count": bootstrap_count,
            "register_count": register_count,
            "required_register_count": len(mutation_ready_targets),
            "guard_check_ok_count": check_ok_count,
            "check_ok_count": check_ok_count,
            "required_check_ok_count": len(mutation_ready_targets),
            "planned_count": planned_count,
            "blocked_count": blocked_count,
            "capture_ready_count": capture_ready_count,
            "capture_missing_count": unverified_count,
            "local_reconcile_required_count": local_reconcile_required_count,
            "mismatch_count": mismatch_count,
            "sparse_snapshot_diff_count": sparse_snapshot_diff_count,
            "unverified_count": unverified_count,
            "review_blocker_count": len(review_blocker_list),
            "review_blockers": review_blocker_list,
            "record_status_counts": record_status_counts,
            "target_digest": target_digest,
        },
        "records": all_records,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "issue_count", "review_blocker_count", "physical_property_count", "portfolio_reporting_target_count", "known_lofty_property_id_count", "current_manager_live_target_count", "current_manager_mutation_ready_count", "current_manager_mutation_unavailable_count", "current_manager_unavailable_count", "native_unavailable_count", "bootstrap_count", "register_count", "check_ok_count", "guard_check_ok_count", "unverified_count", "mismatch_count")}, indent=2, sort_keys=True))
    return 0 if status == "ok" else 2 if status == "review" else 1


if __name__ == "__main__":
    raise SystemExit(main())
