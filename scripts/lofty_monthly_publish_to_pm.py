#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lofty_index_status import is_active_index_status, is_excluded_index_status, normalize_index_status
from lofty_monthly_exclusions import DEFAULT_MANUAL_EXCLUDED_PROPERTIES
from lofty_property_paths import display_name_for_property_path, public_dir_for_property, resolve_index_property_path

UPDATES_DIR_NAME = "00 - README & Property Snapshot"
SNAPSHOT_DIR_NAME = UPDATES_DIR_NAME
ROOT = Path(__file__).absolute().parents[1]
DEFAULT_SKILL_MAP = ROOT / "skills/lofty-pm/config/property_update_map.json"
YHOME_EXCLUDE_MARKERS = ("sold", "selling", "closed", "delisted")
DEFAULT_LISTING_UPDATE_POLICY = ROOT / "config/lofty_listing_update_policy.json"
PROPERTY_PAYLOAD_SUFFIXES = (
    "update-manager-property.payload.json",
    "send-property-updates.payload.json",
    "financial.update-manager-property.payload.json",
    "financial.update-manager-property.payload.patch.json",
    "financial.send-property-updates.payload.json",
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
NATIVE_OWNER_EMAIL_DISABLED_REASON = (
    "native Lofty owner email disabled: send-property-updates emails the saved full updates field; "
    "use the non-native reviewed email workflow"
)
NATIVE_OWNER_EMAIL_OVERRIDE_ENV = "LOFTY_ALLOW_NATIVE_OWNER_EMAIL_FULL_FIELD_RISK"
NATIVE_OWNER_EMAIL_SAFE_ENV = "LOFTY_ALLOW_NATIVE_OWNER_EMAIL_SIGNAL_ONLY"
SAFE_MONTHLY_CRON_DRY_RUN_COMMAND = (
    "DRY_RUN=1 SEND_OWNER_EMAILS=0 PUBLISH_LOFTY_PM_UPDATES=0 "
    "RUN_LOFTY_GUARDED_APPLY=1 APPLY_LOFTY_GUARDED_UPDATES=0 bash scripts/baselane_financials_monthly_cron.sh"
)
STALE_GUARDED_APPLY_ACTIONS = {
    "Capture/register live Lofty UPDATES.md fetch with lofty-updates-guard before applying.": "UPDATES.md",
    "Capture/register live Lofty FINANCIALS.md fetch with lofty-live-file-guard before applying.": "FINANCIALS.md",
}
UNSAFE_FINANCIAL_PATCH_KEYS = {"updates", "updatesDiff"}
SAFE_LIVE_FINANCIAL_PATCH_KEYS = {
    "cash_flow",
    "cashflow_per_unit",
    "coc",
    "current_loan",
    "is_occupied",
    "monthly_loan_repayment",
    "projected_annual_cash_flow",
    "projected_rental_yield",
    "total_investment",
}
LIVE_FINANCIAL_CAPTURE_READY_STATUSES = {
    "guard_ok",
    "guard_ok_live_distribution",
    "guard_ok_no_distribution_target",
    "needs_reconcile",
}
LIVE_FINANCIAL_CAPTURE_GUARD_OK_STATUSES = {
    "guard_ok_live_distribution",
    "guard_ok_no_distribution_target",
}
SKIP_GET_PREFLIGHT_ENV = "LOFTY_SKIP_GET_MANAGER_PROPERTIES_PREFLIGHT"
DESCRIPTION_CHECK_BLOCKING_STATUSES = {"missing", "stale", "inaccurate"}
CANONICAL_MONTHLY_RUNTIME_MAP_NAME = "baselane_financials_monthly_lofty_pm_runtime_map.json"


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "missing", "path": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"status": "unreadable", "path": str(path), "error": str(exc)}
    return data if isinstance(data, dict) else {"status": "unreadable", "path": str(path), "error": "not a JSON object"}


def portfolio_scope_expected_count(review_candidate_packet: Path | None, live_financial_capture: Path | None) -> int | None:
    counts: list[int] = []
    if review_candidate_packet:
        packet = read_json(review_candidate_packet)
        try:
            packet_count = int(packet.get("property_count") or 0)
        except (TypeError, ValueError):
            packet_count = 0
        packet_records = packet.get("records") if isinstance(packet.get("records"), list) else []
        if packet_count > 0:
            counts.append(packet_count)
        if packet_records:
            counts.append(len(packet_records))
    if live_financial_capture:
        capture = read_json(live_financial_capture)
        try:
            target_count = int(capture.get("target_count") or 0)
        except (TypeError, ValueError):
            target_count = 0
        capture_records = capture.get("records") if isinstance(capture.get("records"), list) else []
        if target_count > 0:
            counts.append(target_count)
        if capture_records:
            counts.append(len(capture_records))
    return max(counts) if counts else None


def runtime_map_scope_guard(
    runtime_map: Path,
    property_count: int,
    review_candidate_packet: Path | None,
    live_financial_capture: Path | None,
) -> tuple[Path, str | None, int | None]:
    expected_count = portfolio_scope_expected_count(review_candidate_packet, live_financial_capture)
    if runtime_map.name != CANONICAL_MONTHLY_RUNTIME_MAP_NAME or expected_count is None or expected_count < 10:
        return runtime_map, None, expected_count
    minimum_portfolio_count = max(2, expected_count // 2)
    if property_count >= minimum_portfolio_count:
        return runtime_map, None, expected_count
    sidecar = runtime_map.with_name(f"{runtime_map.stem}.targeted-subset-blocked{runtime_map.suffix}")
    issue = (
        "canonical_runtime_map_subset_refused:"
        f" properties={property_count}, expected_portfolio_count={expected_count};"
        " pass a targeted --runtime-map path for one-off property runs"
    )
    return sidecar, issue, expected_count


def comms_workspace_candidates() -> list[Path]:
    candidates: list[Path] = []
    env_path = os.environ.get("COMMS_WORKSPACE")
    if env_path:
        candidates.append(Path(env_path))
    root = Path(__file__).resolve().parents[1]
    candidates.extend(
        [
            root.parent / "workspace-lofty-vp",
            Path("/home/digit/.openclaw/workspace-lofty-vp"),
            root.parent / "workspace-lofty-vp-comms",
            Path("/home/digit/.openclaw/workspace-lofty-vp-comms"),
        ]
    )
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            unique.append(candidate)
            seen.add(key)
    return unique


def resolve_comms_rent_roll_artifact(path: Path | None, run_month: str, suffix: str) -> Path | None:
    if path is None:
        return None
    if path.is_file():
        return path
    if not run_month:
        return path
    for comms_workspace in comms_workspace_candidates():
        candidate = comms_workspace / "updates" / f"{run_month}-{suffix}"
        if candidate.is_file():
            return candidate
    return path


def parse_accounting_money(value: object) -> float | None:
    raw = "" if value is None else str(value).strip()
    if not raw or raw in {"-", "$ -", "$ - -"}:
        return None
    negative = "(" in raw and ")" in raw
    cleaned = re.sub(r"[^0-9.\-]", "", raw)
    if not cleaned or cleaned in {"-", "."}:
        return None
    try:
        amount = float(cleaned)
    except ValueError:
        return None
    return -abs(amount) if negative else amount


def load_distribution_guard_inputs(
    review_candidate_packet: Path | None,
    yhome_transition_csv: Path | None,
    previous_publish_report: Path | None = None,
    distribution_eligibility_overrides: Path | None = None,
    run_month: str | None = None,
) -> dict[str, Any]:
    packet = read_json(review_candidate_packet) if review_candidate_packet else {"status": "not_configured"}
    records = packet.get("records") if isinstance(packet.get("records"), list) else []
    packet_by_financials: dict[str, dict[str, Any]] = {}
    packet_by_name: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        financials_md = str(record.get("financials_md") or "").strip()
        if financials_md:
            packet_by_financials[financials_md] = record
        for name in (record.get("property_name"), record.get("managed_name"), record.get("input_property_name")):
            key = normalize(str(name or ""))
            if key:
                packet_by_name[key] = record

    yhome_rows: list[dict[str, str]] = []
    yhome_status = "not_configured"
    if yhome_transition_csv:
        if not yhome_transition_csv.is_file():
            yhome_status = "missing"
        else:
            try:
                with yhome_transition_csv.open(newline="", encoding="utf-8-sig") as handle:
                    yhome_rows = list(csv.DictReader(handle))
                yhome_status = "ok"
            except OSError:
                yhome_status = "unreadable"
    previous_by_id: dict[str, dict[str, Any]] = {}
    previous_by_name: dict[str, dict[str, Any]] = {}
    previous = read_json(previous_publish_report) if previous_publish_report else {}
    previous_results = previous.get("financial_publish_results") if isinstance(previous.get("financial_publish_results"), list) else []
    for result in previous_results:
        if not isinstance(result, dict) or not isinstance(result.get("distribution_guard"), dict):
            continue
        property_id = str(result.get("lofty_property_id") or "").strip()
        property_name = normalize(str(result.get("property_name") or ""))
        if property_id:
            previous_by_id[property_id] = result["distribution_guard"]
        if property_name:
            previous_by_name[property_name] = result["distribution_guard"]

    eligibility_overrides: dict[str, dict[str, Any]] = {}
    eligibility_override_status = "not_configured"
    if distribution_eligibility_overrides:
        payload = read_json(distribution_eligibility_overrides)
        eligibility_override_status = str(payload.get("status") or "missing")
        records = payload.get("records") if isinstance(payload.get("records"), list) else []
        for record in records:
            if not isinstance(record, dict):
                continue
            record_month = str(record.get("run_month") or "").strip()
            if run_month and record_month and record_month != run_month:
                continue
            for name in (record.get("property_name"), record.get("managed_name"), record.get("input_property_name")):
                key = normalize(str(name or ""))
                if key:
                    eligibility_overrides[key] = record
    return {
        "packet_status": packet.get("status"),
        "packet_path": str(review_candidate_packet) if review_candidate_packet else None,
        "packet_by_financials": packet_by_financials,
        "packet_by_name": packet_by_name,
        "yhome_status": yhome_status,
        "yhome_path": str(yhome_transition_csv) if yhome_transition_csv else None,
        "yhome_rows": yhome_rows,
        "previous_publish_report": str(previous_publish_report) if previous_publish_report else None,
        "previous_guard_by_id": previous_by_id,
        "previous_guard_by_name": previous_by_name,
        "distribution_eligibility_overrides": eligibility_overrides,
        "distribution_eligibility_override_status": eligibility_override_status,
    }


def distribution_packet_record(prop: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any] | None:
    financials_md = str(prop.get("financials_md") or "").strip()
    record = inputs.get("packet_by_financials", {}).get(financials_md)
    if isinstance(record, dict):
        return record
    names = [
        normalize(str(prop.get("property_name") or "")),
        normalize(str(prop.get("full_address") or "")),
    ]
    for name in names:
        if not name:
            continue
        exact = inputs.get("packet_by_name", {}).get(name)
        if isinstance(exact, dict):
            return exact
        for candidate_name, candidate in inputs.get("packet_by_name", {}).items():
            if name.startswith(candidate_name) or candidate_name.startswith(name):
                return candidate
    return None


def yhome_net_due_for_property(prop: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    property_names = [
        normalize(str(prop.get("property_name") or "")),
        normalize(str(prop.get("full_address") or "")),
    ]
    for row in inputs.get("yhome_rows", []):
        row_name = normalize(str(row.get("Property") or ""))
        if not row_name:
            continue
        if not any(name and (name.startswith(row_name) or row_name.startswith(name)) for name in property_names):
            continue
        raw = next(
            (value for key, value in row.items() if normalize(str(key)) == "yhome net due to dao"),
            None,
        )
        return {"status": "ok", "value": parse_accounting_money(raw), "property": row.get("Property")}
    return {"status": "not_found", "value": None, "property": None}


def previous_distribution_guard(prop: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any] | None:
    property_id = str(prop.get("lofty_property_id") or "").strip()
    if property_id:
        previous = inputs.get("previous_guard_by_id", {}).get(property_id)
        if isinstance(previous, dict):
            return previous
    property_name = normalize(str(prop.get("property_name") or ""))
    previous = inputs.get("previous_guard_by_name", {}).get(property_name)
    return previous if isinstance(previous, dict) else None


def distribution_is_manually_disabled(prop: dict[str, Any]) -> bool:
    names = (
        normalize(str(prop.get("property_name") or "")),
        normalize(str(prop.get("full_address") or "")),
    )
    return any(
        name and (name.startswith(disabled) or disabled.startswith(name))
        for name in names
        for disabled in map(normalize, DEFAULT_DISTRIBUTION_DISABLED_PROPERTIES)
    )


def property_state(prop: dict[str, Any]) -> str | None:
    for key in ("state", "property_state"):
        value = str(prop.get(key) or "").strip().upper()
        if len(value) == 2:
            return value
    for key in ("property_path", "financials_md", "updates_md"):
        raw = str(prop.get(key) or "")
        parts = Path(raw).parts
        for idx, part in enumerate(parts[:-1]):
            if part == "Real Estate" and idx + 1 < len(parts):
                state = parts[idx + 1].strip().upper()
                if len(state) == 2:
                    return state
    return None


def no_mortgage_responsibility(prop: dict[str, Any]) -> bool:
    names = (
        normalize(str(prop.get("property_name") or "")),
        normalize(str(prop.get("full_address") or "")),
        normalize(str(prop.get("property_path") or "")),
    )
    if property_state(prop) in DEFAULT_NO_MORTGAGE_RESPONSIBILITY_STATES:
        return True
    return any(
        name and no_mortgage and (name.startswith(no_mortgage) or no_mortgage.startswith(name) or no_mortgage in name)
        for name in names
        for no_mortgage in map(normalize, DEFAULT_NO_MORTGAGE_RESPONSIBILITY_PROPERTIES)
    )


def cash_source_guard_disabled(prop: dict[str, Any]) -> bool:
    names = (
        normalize(str(prop.get("property_name") or "")),
        normalize(str(prop.get("full_address") or "")),
        normalize(str(prop.get("property_path") or "")),
    )
    return any(
        name and disabled and (name.startswith(disabled) or disabled.startswith(name) or disabled in name)
        for name in names
        for disabled in map(normalize, DEFAULT_CASH_SOURCE_GUARD_DISABLED_PROPERTIES)
    )


def coownership_distribution_states() -> set[str]:
    raw = os.environ.get("LOFTY_PM_COO_OWNERSHIP_DISTRIBUTION_STATES") or ",".join(DEFAULT_COO_OWNERSHIP_DISTRIBUTION_STATES)
    return {part.strip().upper() for part in raw.split(",") if part.strip()}


def coownership_eco_cash_minimum() -> float:
    raw = os.environ.get("LOFTY_PM_COO_OWNERSHIP_ECO_CASH_MINIMUM")
    if raw is None or not raw.strip():
        return DEFAULT_COO_OWNERSHIP_ECO_CASH_MINIMUM
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_COO_OWNERSHIP_ECO_CASH_MINIMUM


def property_state_from_path(path: object) -> str | None:
    try:
        parts = Path(str(path)).parts
    except (TypeError, ValueError):
        return None
    for index, part in enumerate(parts):
        if part == "Real Estate" and index + 1 < len(parts):
            state = parts[index + 1].strip().upper()
            return state or None
    return None


def coownership_distribution_state(prop: dict[str, Any]) -> str | None:
    state = property_state_from_path(prop.get("financials_md") or prop.get("property_path") or prop.get("updates_md"))
    if not state:
        for value in (prop.get("full_address"), prop.get("property_name"), prop.get("match_key")):
            match = re.search(r",\s*([A-Z]{2})\s+\d{5}(?:-\d{4})?\b", str(value or ""), flags=re.IGNORECASE)
            if match:
                state = match.group(1).upper()
                break
    if state and state in coownership_distribution_states():
        return state
    return None


def live_token_price(live_patch: dict[str, Any]) -> tuple[float | None, str | None]:
    for key in ("initialTokenPrice", "originalTokenPrice", "offeringTokenPrice", "token_price"):
        value = parse_accounting_money(live_patch.get(key))
        if value is not None and value > 0:
            return value, key
    raw_default = os.environ.get("LOFTY_PM_CASH_ON_CASH_TOKEN_PRICE") or "50"
    try:
        default_price = float(raw_default)
    except ValueError:
        default_price = 50.0
    return default_price, "fixed_lofty_token_price"


def lofty_distribution_token_float(live_patch: dict[str, Any]) -> tuple[float | None, str | None]:
    value = parse_accounting_money(live_patch.get("currentTokenFloat"))
    if value is not None and value > 0:
        return value, "currentTokenFloat"

    for key in ("numIssued", "num_issued", "issuedTokens", "issued_tokens"):
        value = parse_accounting_money(live_patch.get(key))
        if value is not None and value > 0:
            return value, f"{key}_currentTokenFloat"

    verified_fields = (
        "verifiedFloatingEquityTokens",
        "verified_floating_equity_tokens",
        "onchainFloatingEquityTokens",
        "onchain_floating_equity_tokens",
    )
    for key in verified_fields:
        value = parse_accounting_money(live_patch.get(key))
        if value is not None and value > 0:
            return value, key

    source = str(
        live_patch.get("floatingEquityTokensSource")
        or live_patch.get("floating_equity_tokens_source")
        or ""
    ).strip().lower()
    verified = live_patch.get("floatingEquityTokensVerified") or live_patch.get("floating_equity_tokens_verified")
    if verified is True or source in {"onchain", "on-chain", "verified_onchain", "verified-onchain"}:
        for key in ("floatingEquityTokens", "floating_equity_tokens"):
            value = parse_accounting_money(live_patch.get(key))
            if value is not None and value > 0:
                return value, key

    token_price, _token_price_source = live_token_price(live_patch)
    total_investment = parse_accounting_money(live_patch.get("total_investment"))
    if total_investment is not None and total_investment > 0 and token_price is not None and token_price > 0:
        return round(total_investment / token_price, 8), "total_investment_implied_currentTokenFloat"
    return None, None


def live_token_denominator_inputs(live_patch: dict[str, Any]) -> tuple[float | None, float | None, str | None, str | None]:
    token_source = None
    token_count, token_source = lofty_distribution_token_float(live_patch)
    if token_count is None:
        token_count = (
            parse_accounting_money(live_patch.get("tokens"))
            or parse_accounting_money(live_patch.get("number_of_tokens"))
            or parse_accounting_money(live_patch.get("numberOfTokens"))
        )
        for key in ("tokens", "number_of_tokens", "numberOfTokens"):
            if parse_accounting_money(live_patch.get(key)) == token_count:
                token_source = key
                break
    token_price, token_price_source = live_token_price(live_patch)
    return token_count, token_price, token_source, token_price_source


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


def live_investment_denominator(live_patch: dict[str, Any]) -> float | None:
    token_count, token_price, _token_source, _token_price_source = live_token_denominator_inputs(live_patch)
    if token_count is None or token_count <= 0 or token_price is None or token_price <= 0:
        return None
    return round(token_count * token_price, 2)


def percent_of_live_investment(amount: float | None, live_patch: dict[str, Any]) -> float | None:
    if amount is None:
        return None
    if amount <= 0:
        return 0.0
    denominator = live_investment_denominator(live_patch)
    if denominator is None:
        return None
    return round((amount / denominator) * 100, 2)


def current_rental_yield_percent(annual_cash_flow: float | None, live_patch: dict[str, Any]) -> float | None:
    return percent_of_live_investment(annual_cash_flow, live_patch)


def cash_on_cash_percent(annual_cash_flow: float | None, live_patch: dict[str, Any]) -> float | None:
    return percent_of_live_investment(annual_cash_flow, live_patch)


def live_cashflow_per_unit_annual_cash_flow(live_patch: dict[str, Any]) -> tuple[float | None, str | None]:
    rows = live_patch.get("cashflow_per_unit")
    if not isinstance(rows, list):
        return None, None
    monthly_values = [
        parse_accounting_money(row.get("monthly_cash_flow"))
        for row in rows
        if isinstance(row, dict)
    ]
    monthly_values = [value for value in monthly_values if value is not None]
    if not monthly_values:
        return None, None
    return round(sum(monthly_values) * 12, 2), "live_cashflow_per_unit_sum"


def combined_operating_cash_clearance(
    lofty_operating_reserve: float | None,
    eco_operating_cash: float | None,
    maintenance_reserve: float | None,
) -> tuple[float | None, bool | None]:
    if lofty_operating_reserve is None or eco_operating_cash is None or maintenance_reserve is None:
        return None, None
    combined = round(lofty_operating_reserve + eco_operating_cash, 2)
    return combined, combined > maintenance_reserve


def cash_source_guard_sources(
    source_values: dict[str, float | None],
    maintenance_reserve: float | None,
) -> tuple[list[str], float | None, bool | None]:
    combined_cash, reserve_clear = combined_operating_cash_clearance(
        source_values.get("lofty_operating_reserve"),
        source_values.get("eco_operating_cash_full_column_e"),
        maintenance_reserve,
    )
    if reserve_clear is True:
        return [], combined_cash, reserve_clear
    sources = [name for name, value in source_values.items() if value is not None and value <= 0]
    if reserve_clear is False and "combined_operating_cash_below_maintenance_reserve" not in sources:
        sources.append("combined_operating_cash_below_maintenance_reserve")
    return sources, combined_cash, reserve_clear


def eco_operating_cash_for_distribution(prop: dict[str, Any], summary: dict[str, Any]) -> tuple[float | None, str]:
    total_spendable = parse_accounting_money(summary.get("total_dao_spendable_cash"))
    if total_spendable is not None:
        return total_spendable, "total_dao_spendable_cash"
    eco_unrestricted = parse_accounting_money(summary.get("eco_held_unrestricted_cash"))
    if eco_unrestricted is not None:
        return eco_unrestricted, "eco_held_unrestricted_cash"
    if coownership_distribution_state(prop):
        net_of_accruals = parse_accounting_money(summary.get("eco_gl_column_e_net_of_accruals"))
        if net_of_accruals is not None:
            return net_of_accruals, "eco_gl_column_e_net_of_accruals"
    full_column_value = parse_accounting_money(summary.get("eco_gl_column_e_sum"))
    if full_column_value is not None:
        return full_column_value, "eco_gl_column_e_sum"
    return parse_accounting_money(summary.get("eco_gl_column_e_sum_as_of_month")), "eco_gl_column_e_sum_as_of_month"


def zero_cashflow_per_unit(live_patch: dict[str, Any]) -> list[dict[str, Any]] | None:
    rows = live_patch.get("cashflow_per_unit")
    if not isinstance(rows, list):
        return None
    zeroed: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        next_row = dict(row)
        next_row["monthly_cash_flow"] = 0
        next_row["occupied"] = False
        zeroed.append(next_row)
    return zeroed or None


def normalize_cashflow_per_unit(
    live_patch: dict[str, Any],
    annual_cash_flow: float,
) -> list[dict[str, Any]] | None:
    rows = live_patch.get("cashflow_per_unit")
    if not isinstance(rows, list):
        return None
    usable_rows = [row for row in rows if isinstance(row, dict)]
    if not usable_rows:
        return None
    target_monthly = round(max(annual_cash_flow, 0.0) / 12, 2)
    existing_values = [
        max(parse_accounting_money(row.get("monthly_cash_flow")) or 0.0, 0.0)
        for row in usable_rows
    ]
    existing_total = round(sum(existing_values), 2)
    if abs(existing_total - target_monthly) <= 0.01 and all(row.get("occupied") is bool(target_monthly > 0) for row in usable_rows):
        return None
    positive_total = sum(existing_values)
    if positive_total > 0:
        allocations = [round(target_monthly * value / positive_total, 2) for value in existing_values]
    else:
        occupied_indexes = [
            index
            for index, row in enumerate(usable_rows)
            if row.get("occupied") is not False
        ] or list(range(len(usable_rows)))
        allocations = [0.0 for _row in usable_rows]
        even_amount = round(target_monthly / len(occupied_indexes), 2) if occupied_indexes else 0.0
        for index in occupied_indexes:
            allocations[index] = even_amount
    if allocations:
        allocations[-1] = round(allocations[-1] + target_monthly - round(sum(allocations), 2), 2)
    normalized: list[dict[str, Any]] = []
    for row, allocation in zip(usable_rows, allocations):
        next_row = dict(row)
        next_row["monthly_cash_flow"] = allocation
        next_row["occupied"] = bool(target_monthly > 0)
        normalized.append(next_row)
    return normalized


def build_distribution_guard_patch(
    prop: dict[str, Any],
    parsed_patch: dict[str, Any],
    live_patch: dict[str, Any],
    inputs: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    record = distribution_packet_record(prop, inputs)
    summary = record.get("monthly_financial_summary") if isinstance(record, dict) else None
    summary = summary if isinstance(summary, dict) else {}
    lofty_operating_reserve = parse_accounting_money(summary.get("lofty_curr_maintenance_reserve"))
    eco_operating_cash, eco_operating_cash_source = eco_operating_cash_for_distribution(prop, summary)
    yhome = yhome_net_due_for_property(prop, inputs)
    coownership_state = coownership_distribution_state(prop)
    maintenance_reserve = coownership_eco_cash_minimum()
    source_values = {
        "lofty_operating_reserve": lofty_operating_reserve,
        "eco_operating_cash_full_column_e": eco_operating_cash,
        "yhome_net_due_to_dao": yhome.get("value"),
    }
    distribution_source_values = {
        "lofty_operating_reserve": lofty_operating_reserve,
        "eco_operating_cash_distribution_period": eco_operating_cash,
        "eco_operating_cash_distribution_source": eco_operating_cash_source,
        "yhome_net_due_to_dao": yhome.get("value"),
    }
    missing_required = [
        name
        for name in ("lofty_operating_reserve", "eco_operating_cash_full_column_e")
        if source_values[name] is None
    ]
    reviewed_annual_cash_flow = parse_accounting_money(parsed_patch.get("projected_annual_cash_flow"))
    reviewed_annual_cash_flow_source = "reviewed_financials_patch" if reviewed_annual_cash_flow is not None else None
    live_unit_annual_cash_flow, live_unit_annual_cash_flow_source = live_cashflow_per_unit_annual_cash_flow(live_patch)
    use_live_unit_annual_cash_flow = False
    annual_cash_flow = reviewed_annual_cash_flow
    annual_cash_flow_source = reviewed_annual_cash_flow_source
    utilities_before = parse_accounting_money(live_patch.get("utilities"))
    if annual_cash_flow is None:
        missing_required.append("projected_annual_cash_flow")
    cash_guard_disabled = cash_source_guard_disabled(prop)
    negative_sources, combined_operating_cash, reserve_clear = cash_source_guard_sources(source_values, maintenance_reserve)
    if cash_guard_disabled:
        negative_sources = []
    if missing_required:
        return {}, {
            "status": "blocked_missing_required_source",
            "missing_required_sources": missing_required,
            "source_values": source_values,
            "distribution_source_values": distribution_source_values,
            "eco_operating_cash_distribution_source": eco_operating_cash_source,
            "coownership_distribution_state": coownership_state,
            "coownership_eco_cash_minimum": maintenance_reserve if coownership_state else None,
            "maintenance_reserve": maintenance_reserve,
            "combined_operating_cash": combined_operating_cash,
            "combined_operating_cash_clears_maintenance_reserve": reserve_clear,
            "yhome_status": yhome.get("status"),
            "negative_sources": negative_sources,
            "live_cashflow_per_unit_annual_cash_flow": live_unit_annual_cash_flow,
            "live_cashflow_per_unit_annual_cash_flow_source": live_unit_annual_cash_flow_source,
            "live_cashflow_per_unit_used": use_live_unit_annual_cash_flow,
            "projected_annual_cash_flow_source": annual_cash_flow_source,
        }

    cash_flow_before = parse_accounting_money(parsed_patch.get("cash_flow")) or 0.0
    guard_active = bool(negative_sources)
    manual_disable = distribution_is_manually_disabled(prop)
    previous_guard = previous_distribution_guard(prop, inputs)
    previous_guard_active = bool(previous_guard and previous_guard.get("status") == "guarded_zero_distribution")
    previous_offset = parse_accounting_money(previous_guard.get("utilities_annual_offset")) if previous_guard_active else 0.0
    previous_offset = round(max(previous_offset or 0.0, 0.0), 2)
    retained_capital = parse_accounting_money(summary.get("retained_capital")) or 0.0
    retained_capital_offset = round(abs(min(retained_capital, 0.0)), 2)
    previous_retained_capital_offset = parse_accounting_money(previous_guard.get("retained_capital_utilities_offset")) if previous_guard else 0.0
    previous_retained_capital_offset = round(max(previous_retained_capital_offset or 0.0, 0.0), 2)
    # Distribution eligibility must not rewrite real operating-expense categories.
    # Earlier runs used Utilities as a balancing field, which corrupted the public
    # financial model when a stale guard offset survived a later enablement run.
    utilities_adjustment = 0.0
    utilities_base_before_guard_offset = round(utilities_before or 0.0, 2)
    utilities_base_source = "live_utilities_read_only"
    annual_offset = 0.0
    restored_annual_cash_flow = annual_cash_flow
    token_count, token_price, token_count_source, token_price_source = live_token_denominator_inputs(live_patch)
    missing_authoritative_floating_tokens = bool(
        coownership_state
        and (restored_annual_cash_flow or 0.0) > 0
        and not authoritative_floating_token_source(token_count_source)
    )
    effective_annual_cash_flow = (
        0.0
        if guard_active or manual_disable or missing_authoritative_floating_tokens
        else max(restored_annual_cash_flow or 0.0, 0.0)
    )
    effective_cash_flow = effective_annual_cash_flow
    current_yield = current_rental_yield_percent(effective_annual_cash_flow, live_patch)
    current_coc = cash_on_cash_percent(effective_annual_cash_flow, live_patch)
    token_denominator = live_investment_denominator(live_patch)
    guarded_patch: dict[str, Any] = {
        "cash_flow": effective_cash_flow,
        "is_occupied": False if guard_active or manual_disable else bool(effective_annual_cash_flow > 0),
        "projected_annual_cash_flow": effective_annual_cash_flow,
    }
    no_mortgage = no_mortgage_responsibility(prop)
    current_loan_before = parse_accounting_money(live_patch.get("current_loan"))
    monthly_loan_repayment_before = parse_accounting_money(live_patch.get("monthly_loan_repayment"))
    if no_mortgage and (current_loan_before is None or abs(current_loan_before) > 0.005):
        guarded_patch["current_loan"] = 0.0
    if no_mortgage and (monthly_loan_repayment_before is None or abs(monthly_loan_repayment_before) > 0.005):
        guarded_patch["monthly_loan_repayment"] = 0.0
    if guard_active or manual_disable or effective_annual_cash_flow <= 0:
        zeroed_units = zero_cashflow_per_unit(live_patch)
        if zeroed_units is not None:
            guarded_patch["cashflow_per_unit"] = zeroed_units
    else:
        normalized_units = normalize_cashflow_per_unit(live_patch, effective_annual_cash_flow)
        if normalized_units is not None:
            guarded_patch["cashflow_per_unit"] = normalized_units
    if current_coc is not None:
        guarded_patch["coc"] = current_coc
    if current_yield is not None:
        guarded_patch["projected_rental_yield"] = current_yield
    evidence = {
        "status": "guarded_zero_distribution" if guard_active else "distribution_disabled_manual_override" if manual_disable else "blocked_missing_authoritative_floating_equity_tokens" if missing_authoritative_floating_tokens else "distribution_enabled" if guarded_patch["is_occupied"] else "distribution_disabled_nonpositive_annual_cash_flow",
        "source_values": source_values,
        "distribution_source_values": distribution_source_values,
        "eco_operating_cash_distribution_source": eco_operating_cash_source,
        "negative_sources": negative_sources,
        "cash_source_guard_sources": negative_sources,
        "cash_source_guard_disabled": cash_guard_disabled,
        "coownership_distribution_state": coownership_state,
        "coownership_eco_cash_minimum": maintenance_reserve if coownership_state else None,
        "maintenance_reserve": maintenance_reserve,
        "combined_operating_cash": combined_operating_cash,
        "combined_operating_cash_clears_maintenance_reserve": reserve_clear,
        "manual_distribution_disable": manual_disable,
        "missing_authoritative_floating_equity_tokens": missing_authoritative_floating_tokens,
        "yhome_status": yhome.get("status"),
        "yhome_property": yhome.get("property"),
        "cash_flow_before_guard": cash_flow_before,
        "cash_flow_after_guard": guarded_patch["cash_flow"],
        "current_month_distribution_after_guard": round(guarded_patch["cash_flow"] / 12, 2),
        "cashflow_per_unit_normalized": "cashflow_per_unit" in guarded_patch and not (guard_active or manual_disable or effective_annual_cash_flow <= 0),
        "reviewed_projected_annual_cash_flow": reviewed_annual_cash_flow,
        "live_cashflow_per_unit_annual_cash_flow": live_unit_annual_cash_flow,
        "live_cashflow_per_unit_annual_cash_flow_source": live_unit_annual_cash_flow_source,
        "live_cashflow_per_unit_used": use_live_unit_annual_cash_flow,
        "projected_annual_cash_flow_before_guard": annual_cash_flow,
        "projected_annual_cash_flow_source": annual_cash_flow_source,
        "projected_annual_cash_flow_after_prior_offset_removal": restored_annual_cash_flow,
        "projected_annual_cash_flow_after_guard": effective_annual_cash_flow,
        "cash_on_cash_return_after_guard": current_coc,
        "projected_rental_yield_after_guard": current_yield,
        "cash_on_cash_formula": "(Current Month Distribution x 12) / ((currentTokenFloat/numIssued_currentTokenFloat when supplied by Lofty, otherwise verified on-chain Floating Equity Tokens) x $50 Token Price); total token supply is not a valid co-ownership denominator",
        "cash_on_cash_token_count": token_count,
        "cash_on_cash_token_count_source": token_count_source,
        "cash_on_cash_token_price": token_price,
        "cash_on_cash_token_price_source": token_price_source,
        "cash_on_cash_denominator": token_denominator,
        "no_mortgage_responsibility": no_mortgage,
        "current_loan_before_guard": current_loan_before,
        "current_loan_after_guard": guarded_patch.get("current_loan", current_loan_before),
        "monthly_loan_repayment_before_guard": monthly_loan_repayment_before,
        "monthly_loan_repayment_after_guard": guarded_patch.get("monthly_loan_repayment", monthly_loan_repayment_before),
        "cash_flow_policy": "Lofty property-owners API cash_flow is annualized; the UI displays Current Month Distribution as cash_flow divided by 12. Reviewed prior-month CF/FINANCIALS values are the authoritative source for Annual Cash Flow; live per-unit monthly cashflow rows are normalized from reviewed Annual Cash Flow and are never used as the source for distribution math. Cash-on-cash return is (Current Month Distribution x 12) / (currentTokenFloat/numIssued when supplied by Lofty, otherwise verified on-chain Floating Equity Tokens, otherwise Number of Tokens, x fixed $50 Lofty Token Price); current rental yield and enable distributions follow the same guarded Annual Cash Flow",
        "utilities_before_guard": utilities_before,
        "utilities_base_before_guard_offset": utilities_base_before_guard_offset,
        "utilities_base_source": utilities_base_source,
        "utilities_annual_offset": annual_offset,
        "utilities_adjustment_this_run": utilities_adjustment,
        "utilities_after_guard": utilities_before,
        "previous_guard_active": previous_guard_active,
        "previous_utilities_annual_offset": previous_offset,
        "retained_capital": retained_capital,
        "retained_capital_utilities_offset": retained_capital_offset,
        "previous_retained_capital_utilities_offset": previous_retained_capital_offset,
        "prior_offset_removed": False,
        "is_occupied_after_guard": guarded_patch["is_occupied"],
        "balancing_policy": "distribution guards change only distribution fields; operating-expense categories remain source-backed and read-only. Lofty API cash_flow equals guarded Annual Cash Flow, and the UI Current Month Distribution is cash_flow divided by 12; cash-on-cash return, current rental yield, projected annual cash flow, and enable distributions are driven by guarded Annual Cash Flow",
    }
    return guarded_patch, evidence


def load_review_candidate_sources(path: Path | None, run_month: str) -> dict[str, Path]:
    if not path or not path.is_file():
        return {}
    data = read_json(path)
    records = data.get("records") if isinstance(data.get("records"), list) else []
    sources: dict[str, Path] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        record_month = str(record.get("run_month") or record.get("month") or "").strip()
        if run_month and record_month and record_month != run_month:
            continue
        target = str(record.get("update_approval_target") or "").strip()
        if not target:
            continue
        target_path = Path(target)
        if not target_path.is_file():
            continue
        for key in (
            str(record.get("lofty_property_id") or "").strip(),
            str(record.get("property_name") or "").strip().lower(),
            str(record.get("updates_md") or "").strip(),
        ):
            if key:
                sources[key] = target_path
    return sources


def approved_update_source_for(prop: dict[str, Any], sources: dict[str, Path]) -> Path | None:
    return (
        sources.get(str(prop.get("lofty_property_id") or "").strip())
        or sources.get(str(prop.get("property_name") or "").strip().lower())
        or sources.get(str(prop.get("updates_md") or "").strip())
    )


def stable_digest(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def sha256ish(value: object) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", str(value or "")))


def parse_iso_datetime(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def posted_at_valid(value: object, run_month: str) -> bool:
    parsed = parse_iso_datetime(value)
    if parsed is None:
        return False
    if not run_month:
        return True
    try:
        run_year, run_month_number = (int(part) for part in run_month.split("-", 1))
    except (TypeError, ValueError):
        return False
    close_year = run_year + (1 if run_month_number == 12 else 0)
    close_month_number = 1 if run_month_number == 12 else run_month_number + 1
    return parsed.strftime("%Y-%m") in {
        f"{run_year:04d}-{run_month_number:02d}",
        f"{close_year:04d}-{close_month_number:02d}",
    }


def guild_report_digest_valid(data: dict[str, Any]) -> bool:
    digest = str(data.get("digest") or "")
    if not sha256ish(digest):
        return False
    payload = {key: value for key, value in data.items() if key not in {"generated_at", "digest"}}
    return stable_digest(payload) == digest


def file_mtime_z(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except OSError:
        return None


def compact_count(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def monthly_readiness_blocked_reason(readiness: dict[str, Any]) -> str:
    actionable = readiness.get("actionable_summary") if isinstance(readiness.get("actionable_summary"), dict) else {}
    primary = actionable.get("primary_blocker") if isinstance(actionable.get("primary_blocker"), dict) else {}
    primary_text = str(primary.get("blocker") or primary.get("class") or "").strip()
    actionable_count = compact_count(actionable.get("actionable_blocker_count"))
    if primary_text:
        return f"monthly readiness owner_email_allowed=false; primary={primary_text}; actionable={actionable_count}"
    return f"monthly readiness owner_email_allowed=false; actionable={actionable_count}"


def readiness_snapshot(path: Path | None) -> dict[str, Any]:
    if not path:
        return {"status": "not_configured", "path": None}
    data = read_json(path)
    snapshot = {
        "path": str(path),
        "status": data.get("status"),
        "owner_email_allowed": data.get("owner_email_allowed"),
        "blocker_count": data.get("blocker_count"),
        "blocked_property_count": data.get("blocked_property_count"),
        "counts": data.get("counts") or {},
        "monthly_comms_gates": data.get("monthly_comms_gates") if isinstance(data.get("monthly_comms_gates"), dict) else {},
        "actionable_summary": data.get("actionable_summary") if isinstance(data.get("actionable_summary"), dict) else {},
    }
    snapshot["digest"] = stable_digest(snapshot)
    snapshot["source_generated_at"] = data.get("generated_at")
    snapshot["source_mtime"] = file_mtime_z(path)
    return snapshot


def normalize(value: str) -> str:
    text = value.lower()
    text = re.sub(r"\blfty\d+\b", " ", text)
    text = text.replace("&", "and")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\bavenue\b", "ave", text)
    text = re.sub(r"\bstreet\b", "st", text)
    text = re.sub(r"\blane\b", "ln", text)
    text = re.sub(r"\bnorth\b", "n", text)
    text = re.sub(r"\bohio\b", "oh", text)
    return re.sub(r"\s+", " ", text).strip()


def policy_name_matches(target: str, key: str) -> bool:
    if not target or not key:
        return False
    if key == target or key in target or target in key:
        return True
    target_tokens = [token for token in target.split() if token != "public"]
    key_tokens = key.split()
    return bool(target_tokens and key_tokens[: len(target_tokens)] == target_tokens)


def money_tokens(value: object) -> set[str]:
    raw = str(value or "").strip()
    if not raw:
        return set()
    try:
        amount = float(raw.replace("$", "").replace(",", ""))
    except ValueError:
        return {raw}
    cents = f"{amount:.2f}"
    whole = str(int(amount)) if amount.is_integer() else cents.rstrip("0").rstrip(".")
    with_commas = f"{amount:,.2f}"
    whole_commas = f"{int(amount):,}" if amount.is_integer() else with_commas.rstrip("0").rstrip(".")
    return {raw, cents, whole, with_commas, whole_commas, f"${cents}", f"${with_commas}", f"${whole}", f"${whole_commas}"}


def load_rent_roll_occupancy(path: Path | None) -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    if not path:
        return {}, {"status": "not_configured", "path": None, "record_count": 0}
    if not path.is_file():
        return {}, {"status": "missing", "path": str(path), "record_count": 0}
    rows: dict[str, dict[str, str]] = {}
    row_count = 0
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                row_count += 1
                property_path = str(row.get("property_path") or "").strip()
                managed_name = str(row.get("managed_name") or "").strip()
                for key in (property_path, Path(property_path).name if property_path else "", managed_name):
                    normalized = normalize(key)
                    if normalized and normalized not in rows:
                        rows[normalized] = row
    except Exception as exc:  # noqa: BLE001
        return {}, {"status": "unreadable", "path": str(path), "record_count": row_count, "error": str(exc)}
    return rows, {
        "status": "ok",
        "path": str(path),
        "record_count": row_count,
        "source_mtime": file_mtime_z(path),
    }


def rent_roll_row_for_property(prop: dict[str, Any], rows: dict[str, dict[str, str]]) -> dict[str, str] | None:
    for key in (
        str(prop.get("property_path") or ""),
        str(prop.get("property_name") or ""),
        str(prop.get("full_address") or ""),
    ):
        row = rows.get(normalize(key))
        if row:
            return row
    return None


def ms_epoch_to_date(value: object) -> str:
    raw = str(value or "").strip()
    if not raw or raw == "0":
        return ""
    try:
        timestamp = int(float(raw))
    except ValueError:
        return ""
    if timestamp <= 0:
        return ""
    try:
        return datetime.fromtimestamp(timestamp / 1000, timezone.utc).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return ""


def description_is_short_term_rental(text: str) -> bool:
    normalized_text = normalize(text)
    return any(
        marker in normalized_text
        for marker in (
            "airbnb",
            "short term rental",
            "short and medium term rental",
            "short medium term rental",
            "vacation rental",
        )
    )


def load_lofty_live_properties(path: Path | None) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if not path:
        return {}, {"status": "not_configured", "path": None, "record_count": 0}
    if not path.is_file():
        return {}, {"status": "missing", "path": str(path), "record_count": 0}
    data = read_json(path)
    records = data.get("records") if isinstance(data.get("records"), list) else []
    by_key: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        for key in (
            record.get("id"),
            record.get("address"),
            record.get("assetUnit"),
        ):
            normalized = normalize(str(key or ""))
            if normalized and normalized not in by_key:
                by_key[normalized] = record
    status = data.get("status") or ("ok" if records else "empty")
    return by_key, {
        "status": status,
        "path": str(path),
        "record_count": len(records),
        "source_mtime": file_mtime_z(path),
    }


def lofty_live_property_for_property(prop: dict[str, Any], rows: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    for key in (
        str(prop.get("lofty_property_id") or ""),
        str(prop.get("property_name") or ""),
        str(prop.get("full_address") or ""),
    ):
        row = rows.get(normalize(key))
        if row:
            return row
    return None


def description_accuracy_status(
    prop: dict[str, Any],
    row: dict[str, str] | None,
    live_property: dict[str, Any] | None = None,
) -> dict[str, Any]:
    description_md = Path(str(prop.get("description_md") or ""))
    result: dict[str, Any] = {
        "property_name": prop.get("property_name"),
        "lofty_property_id": prop.get("lofty_property_id"),
        "description_md": str(description_md) if str(description_md) else None,
        "rent_roll_matched": row is not None,
        "lofty_live_matched": live_property is not None,
        "status": "ok",
        "issues": [],
    }
    if not description_md.is_file():
        result["status"] = "missing"
        result["issues"].append("missing_DESCRIPTION.md")
        return result
    try:
        text = description_md.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        result["status"] = "missing"
        result["issues"].append(f"unreadable_DESCRIPTION.md:{exc}")
        return result
    normalized_text = normalize(text)
    is_short_term_rental = description_is_short_term_rental(text)
    result["short_term_rental"] = is_short_term_rental
    result["description_mtime"] = file_mtime_z(description_md)
    if row is None and live_property is None:
        result["status"] = "stale"
        result["issues"].append("missing_current_rent_roll_or_lofty_live_match")
        return result
    if row is None and live_property is not None:
        live_property_type = normalize(str(live_property.get("property_type") or ""))
        if live_property_type in {"vacation rental", "short term rental"}:
            is_short_term_rental = True
            result["short_term_rental"] = True
        occupancy = str(live_property.get("custom_occupancy") or "").strip()
        is_occupied = live_property.get("is_occupied")
        monthly_rent = str(live_property.get("monthly_rent") or "").strip()
        lease_begins_date = ms_epoch_to_date(live_property.get("lease_begins_date"))
        lease_ends_date = ms_epoch_to_date(live_property.get("lease_ends_date"))
        result["lofty_live"] = {
            "id": live_property.get("id"),
            "assetUnit": live_property.get("assetUnit"),
            "address": live_property.get("address"),
            "property_type": live_property.get("property_type"),
            "is_occupied": is_occupied,
            "custom_occupancy": occupancy,
            "monthly_rent": monthly_rent,
            "lease_begins_date": lease_begins_date,
            "lease_ends_date": lease_ends_date,
        }
        if is_short_term_rental:
            result["lofty_live_validation_basis"] = "short_term_listing_description"
            return result
        if occupancy:
            for token in normalize(occupancy).split():
                if token and token not in normalized_text:
                    result["issues"].append("missing_lofty_live_occupancy")
                    break
        if monthly_rent and monthly_rent not in {"0", "0.0", "0.00"} and not any(token and token in text for token in money_tokens(monthly_rent)):
            result["issues"].append("missing_lofty_live_monthly_rent")
        if lease_begins_date and lease_begins_date not in text:
            result["issues"].append("missing_lofty_live_lease_begins_date")
        if lease_ends_date and lease_ends_date not in text:
            result["issues"].append("missing_lofty_live_lease_ends_date")
        if result["issues"]:
            result["status"] = "inaccurate"
        return result
    occupancy = str(row.get("occupancy_status") or "").strip()
    monthly_rent = str(row.get("monthly_rent") or "").strip()
    exported_on = str(row.get("exported_on") or "").strip()
    result["rent_roll"] = {
        "managed_name": row.get("managed_name"),
        "occupancy_status": occupancy,
        "monthly_rent": monthly_rent,
        "exported_on": exported_on,
        "source_xlsx": row.get("source_xlsx"),
    }
    occupancy_lower = occupancy.lower()
    if "occupied" in occupancy_lower and "occupied" not in normalized_text:
        result["issues"].append("missing_occupancy_status")
    if "vacant" in occupancy_lower and "vacant" not in normalized_text:
        result["issues"].append("missing_occupancy_status")
    unit_match = re.search(r"\b(\d+)\s*/\s*(\d+)\b", occupancy)
    if unit_match and unit_match.group(0).replace(" ", "") not in text.replace(" ", ""):
        result["issues"].append("missing_occupied_unit_count")
    if monthly_rent and not any(token and token in text for token in money_tokens(monthly_rent)):
        result["issues"].append("missing_monthly_rent")
    if result["issues"]:
        result["status"] = "inaccurate"
    return result


def description_check_report(
    properties: list[dict[str, Any]],
    source_report_path: Path | None,
    occupancy_csv_path: Path | None,
    lofty_live_properties_path: Path | None,
    run_month: str,
) -> dict[str, Any]:
    source_report_path = resolve_comms_rent_roll_artifact(source_report_path, run_month, "rent-roll-source.json")
    occupancy_csv_path = resolve_comms_rent_roll_artifact(
        occupancy_csv_path,
        run_month,
        "rent-roll-occupancy-summary.csv",
    )
    if source_report_path is None and occupancy_csv_path is None and lofty_live_properties_path is None:
        return {
            "status": "not_configured",
            "run_month": run_month,
            "source_report": {"status": "not_configured", "path": None},
            "occupancy_summary": {"status": "not_configured", "path": None, "record_count": 0},
            "lofty_live_properties": {"status": "not_configured", "path": None, "record_count": 0},
            "record_count": 0,
            "ok_count": 0,
            "missing_count": 0,
            "stale_count": 0,
            "inaccurate_count": 0,
            "blocking_count": 0,
            "source_issue_count": 0,
            "source_issues": [],
            "records": [],
    }
    source = read_json(source_report_path) if source_report_path else {"status": "not_configured", "path": None}
    source_summary = {
        "status": source.get("status"),
        "path": str(source_report_path) if source_report_path else None,
        "run_month": source.get("run_month"),
        "freshness_status": source.get("freshness_status") or source.get("source_freshness_status"),
        "latest_exported_on": source.get("latest_exported_on"),
        "pending_gap_count": compact_count(source.get("pending_gap_count")),
        "owner_email_allowed": source.get("owner_email_allowed"),
        "live_update_allowed": source.get("live_update_allowed"),
        "source_mtime": file_mtime_z(source_report_path) if source_report_path else None,
    }
    rows, occupancy_summary = load_rent_roll_occupancy(occupancy_csv_path)
    live_rows, lofty_live_summary = load_lofty_live_properties(lofty_live_properties_path)
    records = [
        description_accuracy_status(
            prop,
            rent_roll_row_for_property(prop, rows),
            lofty_live_property_for_property(prop, live_rows),
        )
        for prop in properties
    ]
    source_issues: list[str] = []
    if source_summary["status"] != "ok":
        source_issues.append(f"rent_roll_source_status={source_summary['status']}")
    if source_summary["freshness_status"] != "current":
        source_issues.append(f"rent_roll_freshness_status={source_summary['freshness_status']}")
    if source_summary["run_month"] and run_month and source_summary["run_month"] != run_month:
        source_issues.append(f"rent_roll_run_month_mismatch={source_summary['run_month']}")
    if occupancy_summary["status"] != "ok":
        source_issues.append(f"occupancy_summary_status={occupancy_summary['status']}")
    if lofty_live_properties_path and lofty_live_summary["status"] not in {"ok", "not_configured"}:
        source_issues.append(f"lofty_live_properties_status={lofty_live_summary['status']}")
    blocking_records = [record for record in records if record.get("status") in DESCRIPTION_CHECK_BLOCKING_STATUSES]
    status = "ok" if not source_issues and not blocking_records else "review"
    return {
        "status": status,
        "run_month": run_month,
        "source_report": source_summary,
        "occupancy_summary": occupancy_summary,
        "lofty_live_properties": lofty_live_summary,
        "record_count": len(records),
        "ok_count": sum(1 for record in records if record.get("status") == "ok"),
        "missing_count": sum(1 for record in records if record.get("status") == "missing"),
        "stale_count": sum(1 for record in records if record.get("status") == "stale"),
        "inaccurate_count": sum(1 for record in records if record.get("status") == "inaccurate"),
        "blocking_count": len(blocking_records),
        "source_issue_count": len(source_issues),
        "source_issues": source_issues,
        "records": records,
    }


def property_id_from_href(value: str) -> str:
    match = re.search(r"/property-owners/edit/([A-Z0-9]+)", value or "")
    return match.group(1) if match else ""


def load_index(index_csv: Path) -> list[dict[str, str]]:
    with index_csv.open(newline="", encoding="utf-8") as handle:
        return [row for row in csv.DictReader(handle)]


def load_yhome_transition_exclusions(yhome_csv: Path | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not yhome_csv:
        return [], {"status": "not_configured", "path": None, "excluded_count": 0}
    if not yhome_csv.is_file():
        return [], {"status": "missing", "path": str(yhome_csv), "excluded_count": 0}
    excluded: list[dict[str, Any]] = []
    row_count = 0
    column_b_header = ""
    with yhome_csv.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
        column_b_header = str(header[1] if len(header) > 1 else "").strip()
        property_index = next((idx for idx, name in enumerate(header) if normalize(str(name)) == "property"), 0)
        if len(header) < 2:
            return [], {
                "status": "missing_column_b",
                "path": str(yhome_csv),
                "row_count": 0,
                "excluded_count": 0,
                "column_b_index": 1,
                "column_b_header": column_b_header,
                "column_b_rule": "Yhome Transition Reconciliation column B marks sold/selling/closed/delisted properties",
                "column_b_rule_ok": False,
            }
        for values in reader:
            row_count += 1
            property_name = str(values[property_index] if len(values) > property_index else "").strip()
            new_pm = str(values[1] if len(values) > 1 else "").strip()
            new_pm_normalized = normalize(new_pm)
            if not property_name or not any(marker in new_pm_normalized.split() for marker in YHOME_EXCLUDE_MARKERS):
                continue
            excluded.append(
                {
                    "source": "yhome_transition_reconciliation",
                    "property_name": property_name,
                    "normalized_property": normalize(property_name),
                    "yhome_column_b": new_pm,
                    "exclude_reason": "Yhome Transition Reconciliation column B marks property as sold/selling/closed/delisted",
                }
            )
    return excluded, {
        "status": "ok",
        "path": str(yhome_csv),
        "row_count": row_count,
        "excluded_count": len(excluded),
        "column_b_index": 1,
        "column_b_header": column_b_header,
        "column_b_rule": "Yhome Transition Reconciliation column B marks sold/selling/closed/delisted properties",
        "column_b_marker_count": len(excluded),
        "column_b_rule_ok": len(excluded) > 0,
        "excluded_property_names": [row["property_name"] for row in excluded],
    }


def manual_exclusion_records(names: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "source": "manual_exclusion",
            "property_name": name.strip(),
            "normalized_property": normalize(name),
            "exclude_reason": "manual do-not-update/do-not-email property exclusion",
        }
        for name in names
        if name.strip()
    ]


def listing_update_policy_exclusion_records(policy_path: Path | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not policy_path or not policy_path.is_file():
        return [], {"status": "missing" if policy_path else "not_configured", "path": str(policy_path) if policy_path else None, "excluded_count": 0}
    policy = read_json(policy_path); records = []
    for field, reason in {
        "sold_ignore_listing_updates": "sold/offboarded by listing policy",
        "operational_ignore_listing_updates": "operationally excluded by listing policy",
    }.items():
        for value in policy.get(field) or []:
            name = str((value.get("address") or value.get("property_name") if isinstance(value, dict) else value) or "").strip()
            if name:
                records.append({"source": f"listing_update_policy:{field}", "property_name": name, "normalized_property": normalize(name), "exclude_reason": str((value.get("reason") if isinstance(value, dict) else "") or reason).strip()})
    return records, {"status": "ok", "path": str(policy_path), "excluded_count": len(records), "excluded_property_names": [r["property_name"] for r in records]}


def financial_hold_records(transfer_report_path: Path | None) -> list[dict[str, Any]]:
    if not transfer_report_path or not transfer_report_path.is_file():
        return []
    report = read_json(transfer_report_path)
    details = report.get("property_cash_review_details")
    if not isinstance(details, list):
        return []
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for detail in details:
        if not isinstance(detail, dict):
            continue
        property_name = str(detail.get("property") or detail.get("property_name") or "").strip()
        normalized_property = normalize(property_name)
        if not normalized_property or normalized_property in seen:
            continue
        if str(detail.get("source_clean_status") or "").strip().lower() == "ok":
            continue
        seen.add(normalized_property)
        records.append(
            {
                "source": "transfer_reconciliation_financial_hold",
                "property_name": property_name,
                "normalized_property": normalized_property,
                "exclude_reason": "property financial truth is held pending source-cash review",
            }
        )
    return records


def guarded_apply_exclusion_records(guarded_apply: dict[str, Any]) -> list[dict[str, Any]]:
    guarded_records = guarded_apply.get("records")
    if not isinstance(guarded_records, list):
        return []
    excluded: list[dict[str, Any]] = []
    seen: set[str] = set()
    skipped_statuses = {"skipped_sold", "skipped_closed", "excluded_no_live_update_or_email"}
    for record in guarded_records:
        if not isinstance(record, dict):
            continue
        update_status = str(((record.get("updates") or {}).get("status")) or "").strip().lower()
        financial_status = str(((record.get("financials") or {}).get("status")) or "").strip().lower()
        if update_status not in skipped_statuses and financial_status not in skipped_statuses:
            continue
        property_path_text = str(record.get("property_path") or record.get("input_property_path") or "").strip()
        property_name = str(record.get("property_name") or "").strip()
        if property_path_text:
            property_name = property_name or display_name_for_property_path(Path(property_path_text), {})
        normalized_property = normalize(property_name or property_path_text)
        if not normalized_property or normalized_property in seen:
            continue
        seen.add(normalized_property)
        notes = " ".join(
            str(((record.get(section) or {}).get("notes")) or "")
            for section in ("updates", "financials")
            if isinstance(record.get(section), dict)
        ).lower()
        source = "guarded_apply_exclusion"
        if "source=manual_exclusion" in notes:
            source = "manual_exclusion"
        elif update_status.startswith("skipped_") or financial_status.startswith("skipped_"):
            source = "monthly_index_skipped"
        excluded.append(
            {
                "source": source,
                "property_name": property_name or property_path_text,
                "property_path": property_path_text,
                "normalized_property": normalized_property,
                "index_status": record.get("index_status"),
                "exclude_reason": "guarded apply marked property skipped/excluded; no live update or owner email",
            }
        )
    return excluded


def match_exclusion_guard(property_path: Path, guards: list[dict[str, Any]]) -> dict[str, Any] | None:
    target_names = [property_path.name]
    if property_path.name.lower() == "public":
        target_names.append(property_path.parent.name)
    targets = [normalize(name) for name in target_names]
    targets = [target for target in targets if target]
    if not targets:
        return None
    matches: list[tuple[int, dict[str, Any]]] = []
    for guard in guards:
        key = str(guard.get("normalized_property") or "").strip()
        if not key:
            continue
        for target in targets:
            if policy_name_matches(target, key):
                matches.append((len(key) + (1000 if key == target else 0), guard))
    matches.sort(key=lambda item: item[0], reverse=True)
    return matches[0][1] if matches else None


def append_unmapped_exclusion_records(
    records: list[dict[str, Any]],
    exclusions: list[dict[str, Any]],
    candidates: list[dict[str, str]],
    payload_dir: Path,
) -> None:
    existing_keys = {
        normalize(str(record.get("property_name") or record.get("property_path") or ""))
        for record in records
        if isinstance(record, dict)
    }
    for exclusion in exclusions:
        key = str(exclusion.get("normalized_property") or "").strip()
        if not key or key in existing_keys:
            continue
        property_path_text = str(exclusion.get("property_path") or "").strip()
        property_path = Path(property_path_text or str(exclusion.get("property_name") or ""))
        property_name = str(exclusion.get("property_name") or "").strip()
        if not property_name and property_path_text:
            property_name = display_name_for_property_path(property_path, {})
        property_id, match = match_property_id(property_path, candidates)
        records.append(
            {
                "property_path": property_path_text,
                "property_name": property_name or property_path_text,
                "lofty_property_id": property_id,
                "index_status": exclusion.get("index_status"),
                "status": "excluded_no_live_update_or_email",
                "exclude_source": exclusion.get("source"),
                "exclude_reason": exclusion.get("exclude_reason"),
                "matched_exclusion_property": exclusion.get("property_name"),
                "removed_excluded_payload_files": remove_excluded_property_payloads(payload_dir, property_id),
                **match,
            }
        )
        existing_keys.add(key)


def guild_test_post_snapshot(path: Path | None, run_month: str) -> dict[str, Any]:
    if not path:
        return {"status": "not_configured", "path": None, "valid": False}
    data = read_json(path)
    status = str(data.get("status") or "")
    posted = data.get("posted") is True or data.get("post_status") in {"ok", "sent", "posted"}
    run_month_ok = data.get("run_month") in {None, "", run_month}
    posted_at_ok = posted_at_valid(data.get("posted_at"), run_month)
    digest_ok = guild_report_digest_valid(data)
    valid = status in {"ok", "sent", "posted"} and posted and run_month_ok and posted_at_ok and digest_ok
    selected = data.get("selected") if isinstance(data.get("selected"), dict) else {}
    route_report = data.get("route_report") if isinstance(data.get("route_report"), dict) else {}
    return {
        "path": str(path),
        "status": data.get("status"),
        "posted": posted,
        "post_status": data.get("post_status"),
        "run_month": data.get("run_month"),
        "run_month_matches": run_month_ok,
        "valid": valid,
        "prepared": status == "prepared_not_posted",
        "target": data.get("target") or selected.get("target"),
        "posted_message_id": data.get("posted_message_id"),
        "posted_channel_id": data.get("posted_channel_id"),
        "posted_at": data.get("posted_at"),
        "digest": data.get("digest"),
        "digest_valid": digest_ok,
        "posted_at_month_matches": posted_at_ok,
        "property_name": selected.get("property_name"),
        "selected": selected,
        "route_report": route_report,
        "message_file": data.get("message_file"),
        "envelope_file": data.get("envelope_file"),
        "next_action": data.get("next_action"),
        "snapshot_digest": stable_digest(data),
    }


def property_id_candidates(portfolio_map: Path | None, skill_map: Path | None) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    if portfolio_map and portfolio_map.is_file():
        data = json.loads(portfolio_map.read_text(encoding="utf-8"))
        rows = data.get("properties") if isinstance(data, dict) else data
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            property_id = property_id_from_href(str(row.get("editHref") or ""))
            name = str(row.get("name") or "")
            if property_id and name:
                candidates.append({"source": "portfolio_map", "key": name, "property_id": property_id, "normalized": normalize(name)})
    if skill_map and skill_map.is_file():
        data = json.loads(skill_map.read_text(encoding="utf-8"))
        row_groups = []
        if isinstance(data, dict):
            row_groups.extend(
                [
                    ("skill_map", data.get("properties") or []),
                    ("skill_map_unresolved", data.get("unresolved") or []),
                ]
            )
        else:
            row_groups.append(("skill_map", data))
        for source, rows in row_groups:
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                property_id = str(row.get("lofty_property_id") or "")
                for key_name in ("full_address", "property_name", "slug"):
                    key = str(row.get(key_name) or "")
                    if property_id and key:
                        candidates.append({"source": source, "key": key, "property_id": property_id, "normalized": normalize(key)})
    return candidates


def effective_skill_map_path(skill_map: Path | None) -> Path | None:
    if skill_map and skill_map.is_file():
        return skill_map
    if DEFAULT_SKILL_MAP.is_file():
        return DEFAULT_SKILL_MAP
    return skill_map


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
            source_bonus = {"portfolio_map": 100, "skill_map": 50}.get(candidate["source"], 0)
            score = len(key) + (1000 if key == matched_target else 0) + source_bonus
            matches.append((score, candidate))
    matches.sort(key=lambda item: item[0], reverse=True)
    if not matches:
        return None, {"match_status": "unmatched", "normalized_property": target}
    top_score, top = matches[0]
    ambiguous = [candidate for score, candidate in matches if score == top_score and candidate["property_id"] != top["property_id"]]
    if ambiguous:
        return None, {"match_status": "ambiguous", "normalized_property": target, "candidates": [top, *ambiguous]}
    return top["property_id"], {"match_status": "matched", "match_source": top["source"], "match_key": top["key"]}


def guarded_apply_ready(guarded_apply: dict[str, Any]) -> tuple[bool, str]:
    if guarded_apply.get("status") != "ok":
        return False, f"guarded apply status is {guarded_apply.get('status') or 'missing'}"
    if guarded_apply.get("apply") is not True:
        return False, "guarded apply did not run in apply mode"
    return True, "guarded apply ok"


def guarded_apply_ready_for_publish_mode(guarded_apply: dict[str, Any], apply_mode: bool) -> tuple[bool, str]:
    if guarded_apply.get("status") != "ok":
        return False, f"guarded apply status is {guarded_apply.get('status') or 'missing'}"
    if apply_mode and guarded_apply.get("apply") is not True:
        return False, "guarded apply did not run in apply mode"
    return True, "guarded apply ok" if guarded_apply.get("apply") is True else "guarded apply dry-run ok"


def guarded_apply_live_ready(guarded_apply: dict[str, Any]) -> bool:
    return guarded_apply.get("status") == "ok" and guarded_apply.get("apply") is True


def guarded_apply_issue_details(guarded_apply: dict[str, Any]) -> list[str]:
    issues = guarded_apply.get("issues")
    if not isinstance(issues, list):
        return []
    return [str(issue) for issue in issues if str(issue).strip()]


def suppress_deferred_guarded_apply_issues(
    guarded_apply_issues: list[str],
    readiness: dict[str, Any],
) -> tuple[list[str], list[dict[str, Any]]]:
    gates = readiness.get("monthly_comms_gates") if isinstance(readiness.get("monthly_comms_gates"), dict) else {}
    suppress_missing_draft = compact_count(gates.get("missing_monthly_draft_collapsed_by_rent_roll_hold_count")) > 0
    suppress_needs_reviewed = (
        gates.get("owner_gate_updates_deferred_by_rent_roll") is True
        or compact_count(gates.get("needs_reviewed_entry_collapsed_by_rent_roll_hold_count")) > 0
    )
    surfaced: list[str] = []
    suppressed: list[dict[str, Any]] = []
    for issue in guarded_apply_issues:
        issue_text = str(issue)
        if suppress_missing_draft and issue_text.startswith("updates.missing_monthly_draft"):
            suppressed.append(
                {
                    "issue": issue_text,
                    "reason": "collapsed_by_rent_roll_hold",
                    "source": "monthly_readiness.monthly_comms_gates.missing_monthly_draft_collapsed_by_rent_roll_hold_count",
                }
            )
            continue
        if suppress_needs_reviewed and issue_text.startswith("updates.needs_reviewed_entry"):
            suppressed.append(
                {
                    "issue": issue_text,
                    "reason": "deferred_by_rent_roll_hold",
                    "source": "monthly_readiness.monthly_comms_gates.owner_gate_updates_deferred_by_rent_roll",
                }
            )
            continue
        surfaced.append(issue_text)
    return surfaced, suppressed


def guarded_apply_actionable_blockers(guarded_apply: dict[str, Any]) -> list[dict[str, Any]]:
    blockers = guarded_apply.get("actionable_blockers")
    if not isinstance(blockers, list):
        return []
    sanitized: list[dict[str, Any]] = []
    for item in blockers:
        if not isinstance(item, dict):
            continue
        blocker = dict(item)
        target = STALE_GUARDED_APPLY_ACTIONS.get(str(blocker.get("next_action") or ""))
        if target:
            blocker["next_action"] = (
                f"Auth Lofty visible tab (3 tries), then refresh live {target} guard evidence through the safe monthly dry-run. "
                f"Run `{SAFE_MONTHLY_CRON_DRY_RUN_COMMAND}`; this keeps email, Lofty PM publish, and guarded live writes disabled."
            )
        sanitized.append(blocker)
    return sanitized


def publish_primary_blocker(
    issues: list[str],
    guarded_apply_blockers: list[dict[str, Any]],
    guarded_apply_issues: list[str],
    readiness: dict[str, Any],
    send_blocked_reason: str | None,
    include_send_blocker: bool = True,
) -> dict[str, Any] | None:
    if guarded_apply_blockers:
        first = guarded_apply_blockers[0]
        blocker_class = str(first.get("class") or "guarded_apply.review")
        count = compact_count(first.get("count"))
        return {
            "class": blocker_class,
            "blocker": f"guarded_apply:{blocker_class}" + (f"={count}" if count else ""),
            "source": "guarded_apply.actionable_blockers",
            "artifact": "reports/baselane_financials_monthly_guard_audit.json",
            "evidence": "reports/baselane_financials_monthly_guarded_apply.json",
            "hold": "Lofty PM publish and investor email",
            "count": count,
            "first_property": first.get("first_property"),
            "first_target": first.get("first_target"),
            "first_error": first.get("first_error"),
            "next_action": first.get("next_action") or f"Run `{SAFE_MONTHLY_CRON_DRY_RUN_COMMAND}` after refreshing live Lofty guard evidence.",
        }
    if guarded_apply_issues:
        first_issue = str(guarded_apply_issues[0])
        return {
            "class": first_issue.split("=", 1)[0],
            "blocker": f"guarded_apply:{first_issue}",
            "source": "guarded_apply.issues",
            "artifact": "reports/baselane_financials_monthly_guarded_apply.json",
            "evidence": "reports/baselane_financials_monthly_guard_audit.json",
            "hold": "Lofty PM publish and investor email",
            "next_action": f"Run `{SAFE_MONTHLY_CRON_DRY_RUN_COMMAND}` after resolving the guarded apply issue; this keeps external sends disabled.",
        }
    readiness_primary = readiness.get("primary_blocker") if isinstance(readiness.get("primary_blocker"), dict) else {}
    if not readiness_primary and isinstance(readiness.get("actionable_summary"), dict):
        actionable = readiness.get("actionable_summary") or {}
        readiness_primary = actionable.get("primary_blocker") if isinstance(actionable.get("primary_blocker"), dict) else {}
    if readiness_primary and include_send_blocker:
        return {
            "class": readiness_primary.get("class") or readiness_primary.get("blocker") or "monthly_readiness.review",
            "blocker": readiness_primary.get("blocker") or readiness_primary.get("class") or "monthly_readiness.review",
            "source": "monthly_readiness.primary_blocker",
            "artifact": readiness_primary.get("artifact") or "reports/baselane_financials_monthly_readiness.json",
            "evidence": readiness_primary.get("evidence") or "reports/baselane_financials_monthly_readiness.json",
            "hold": readiness_primary.get("hold") or "Lofty PM publish and investor email",
            "next_action": readiness_primary.get("next_action") or readiness_primary.get("action") or f"Run `{SAFE_MONTHLY_CRON_DRY_RUN_COMMAND}` after resolving readiness.",
        }
    if send_blocked_reason and include_send_blocker:
        return {
            "class": "owner_email_send_blocked",
            "blocker": send_blocked_reason,
            "source": "owner_email_send_decision",
            "artifact": "reports/baselane_financials_monthly_lofty_pm_publish.json",
            "evidence": "reports/baselane_monthly_owner_email_send_guard.json",
            "hold": "investor email",
            "next_action": "Resolve the send-blocked reason, rerun the monthly dry-run, and only then consider owner email.",
        }
    if issues:
        first_issue = str(issues[0])
        return {
            "class": first_issue.split(":", 1)[0],
            "blocker": first_issue,
            "source": "publish.issues",
            "artifact": "reports/baselane_financials_monthly_lofty_pm_publish.json",
            "hold": "Lofty PM publish and investor email",
            "next_action": "Resolve the publish issue and rerun the monthly dry-run before any external publish/send.",
        }
    return None


def build_runtime_map(
    rows: list[dict[str, str]],
    candidates: list[dict[str, str]],
    payload_dir: Path,
    run_month: str,
    bootstrap_missing_financials_md: bool,
    excluded_property_guards: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    properties: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    gmp_payload = payload_dir / "manager.get-manager-properties.payload.json"
    payload_dir.mkdir(parents=True, exist_ok=True)
    if not gmp_payload.exists():
        gmp_payload.write_text(json.dumps({"year": str(datetime.now().year), "month": str(datetime.now().month)}, indent=2) + "\n", encoding="utf-8")
    for row in rows:
        property_path, path_resolution = resolve_index_property_path(row)
        property_name = display_name_for_property_path(property_path, path_resolution)
        row_status = normalize_index_status(row.get("status"))
        if is_excluded_index_status(row.get("status")):
            property_id, match = match_property_id(property_path, candidates)
            records.append(
                {
                    "property_path": str(property_path),
                    "property_name": property_name,
                    "lofty_property_id": property_id,
                    "index_status": row_status,
                    "status": "excluded_no_live_update_or_email",
                    "exclude_reason": "property is sold/delisted/closed or explicitly skipped in monthly index",
                    "removed_excluded_payload_files": remove_excluded_property_payloads(payload_dir, property_id),
                    **match,
                    **path_resolution,
                }
            )
            continue
        if not is_active_index_status(row.get("status")):
            continue
        exclusion = match_exclusion_guard(property_path, excluded_property_guards or [])
        if exclusion:
            property_id, match = match_property_id(property_path, candidates)
            records.append(
                {
                    "property_path": str(property_path),
                    "property_name": property_name,
                    "lofty_property_id": property_id,
                    "index_status": row_status,
                    "status": "excluded_no_live_update_or_email",
                    "exclude_source": exclusion.get("source"),
                    "exclude_reason": exclusion.get("exclude_reason"),
                    "matched_exclusion_property": exclusion.get("property_name"),
                    "yhome_column_b": exclusion.get("yhome_column_b"),
                    "removed_excluded_payload_files": remove_excluded_property_payloads(payload_dir, property_id),
                    **match,
                    **path_resolution,
                }
            )
            continue
        public_dir = public_dir_for_property(property_path)
        snapshot_dir = public_dir / SNAPSHOT_DIR_NAME
        updates_md = snapshot_dir / "UPDATES.md"
        financials_md = snapshot_dir / "FINANCIALS.md"
        details_md = snapshot_dir / "DETAILS.md"
        description_md = snapshot_dir / "DESCRIPTION.md"
        property_id, match = match_property_id(property_path, candidates)
        record: dict[str, Any] = {
            "property_path": str(property_path),
            "property_name": property_name,
            "updates_md": str(updates_md),
            "financials_md": str(financials_md),
            "details_md": str(details_md),
            "description_md": str(description_md),
            "lofty_property_id": property_id,
            **path_resolution,
            **match,
        }
        if not property_id:
            record["status"] = "blocked_no_property_id"
            records.append(record)
            continue
        if not updates_md.is_file():
            record["status"] = "blocked_missing_updates_md"
            records.append(record)
            continue
        if not financials_md.is_file():
            record["status"] = "blocked_missing_financials_md"
            record["bootstrap_missing_financials_md_disabled"] = bool(bootstrap_missing_financials_md)
            records.append(record)
            continue
        save_payload = payload_dir / f"{property_id}.update-manager-property.payload.json"
        send_payload = payload_dir / f"{property_id}.send-property-updates.payload.json"
        financial_save_payload = payload_dir / f"{property_id}.financial.update-manager-property.payload.json"
        financial_send_payload = payload_dir / f"{property_id}.financial.send-property-updates.payload.json"
        properties.append(
            {
                "property_name": property_name,
                "full_address": property_name,
                "lofty_property_id": property_id,
                "updates_md": str(updates_md),
                "financials_md": str(financials_md),
                "description_md": str(description_md) if description_md.is_file() else None,
                "save_payload_file": str(save_payload),
                "send_payload_file": str(send_payload),
                "financial_save_payload_file": str(financial_save_payload),
                "financial_send_payload_file": str(financial_send_payload),
                "get_manager_properties_payload_file": str(gmp_payload),
                "slug": normalize(property_path.name).replace(" ", "-"),
            }
        )
        record["status"] = "mapped"
        records.append(record)
    return properties, records


def property_payload_files(payload_dir: Path, property_id: str | None) -> list[Path]:
    if not property_id:
        return []
    return [payload_dir / f"{property_id}.{suffix}" for suffix in PROPERTY_PAYLOAD_SUFFIXES]


def remove_excluded_property_payloads(payload_dir: Path, property_id: str | None) -> list[str]:
    removed: list[str] = []
    for path in property_payload_files(payload_dir, property_id):
        if not path.exists():
            continue
        path.unlink()
        removed.append(str(path))
    return removed


def parse_stdout_json_objects(stdout: str) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    index = 0
    while index < len(stdout):
        start = stdout.find("{", index)
        if start < 0:
            break
        try:
            parsed, end = decoder.raw_decode(stdout[start:])
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(parsed, dict):
            objects.append(parsed)
        index = start + max(end, 1)
    return objects


def publish_send_summary(stdout: str, ok: bool) -> dict[str, Any]:
    objects = parse_stdout_json_objects(stdout)
    summary = next((item for item in reversed(objects) if "will_send" in item and "state_file" in item), {})
    state_file = summary.get("state_file")
    latest_digest = summary.get("latest_digest")
    field_digest = summary.get("field_digest")
    will_send = summary.get("will_send") is True
    dry_run = summary.get("dry_run") is True
    evidence_issues = []
    if will_send and not state_file:
        evidence_issues.append("missing state_file")
    if will_send and not latest_digest:
        evidence_issues.append("missing latest_digest")
    if will_send and not field_digest:
        evidence_issues.append("missing field_digest")
    has_send_evidence = ok and will_send and not dry_run and not evidence_issues
    return {
        "stdout_json_object_count": len(objects),
        "state_file": state_file,
        "latest_digest": latest_digest,
        "field_digest": field_digest,
        "will_send": will_send,
        "skip_send": summary.get("skip_send") is True,
        "send_interval_days": summary.get("send_interval_days"),
        "dry_run": dry_run,
        "listing_update_scope": summary.get("listing_update_scope"),
        "listing_update_guard_ok": summary.get("listing_update_guard_ok"),
        "listing_update_char_count": summary.get("listing_update_char_count"),
        "listing_update_line_count": summary.get("listing_update_line_count"),
        "owner_email_send_evidence": has_send_evidence,
        "owner_email_send_evidence_issues": evidence_issues,
    }


def run_publish(command: list[str]) -> dict[str, Any]:
    env = os.environ.copy()
    env.setdefault(SKIP_GET_PREFLIGHT_ENV, "1")
    timeout_seconds = int(os.environ.get("LOFTY_PUBLISH_SUBPROCESS_TIMEOUT_SECONDS") or 600)
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout_seconds, env=env)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return {
            "command": command,
            "return_code": None,
            "ok": False,
            "error": "publish subprocess timed out",
            "timeout_seconds": timeout_seconds,
            "stdout_tail": stdout[-4000:],
            "stderr_tail": stderr[-4000:],
            "stdout_json_object_count": 0,
            "dry_run": False,
            "will_send": False,
            "owner_email_send_evidence": False,
            "owner_email_send_evidence_issues": [],
        }
    send_summary = publish_send_summary(result.stdout, result.returncode == 0)
    return {
        "command": command,
        "return_code": result.returncode,
        "ok": result.returncode == 0,
        "stdout_tail": result.stdout[-4000:],
        "stderr_tail": result.stderr[-4000:],
        **send_summary,
    }


def run_financial_patch(command: list[str], run_month: str | None = None) -> dict[str, Any]:
    env = os.environ.copy()
    if run_month:
        env["RUN_MONTH"] = run_month
    result = subprocess.run(command, capture_output=True, text=True, timeout=120, env=env)
    patch: dict[str, Any] = {}
    if result.returncode == 0:
        objects = parse_stdout_json_objects(result.stdout)
        patch = objects[-1] if objects else {}
    return {
        "command": command,
        "return_code": result.returncode,
        "ok": result.returncode == 0,
        "stdout_tail": result.stdout[-4000:],
        "stderr_tail": result.stderr[-4000:],
        "patch": patch,
        "field_count": int(patch.get("field_count") or 0) if isinstance(patch, dict) else 0,
        "fields": patch.get("fields") if isinstance(patch, dict) else [],
        "sources": patch.get("sources") if isinstance(patch, dict) else [],
    }


def live_financial_statuses(path: Path | None) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if path is None:
        return {}, {"status": "not_configured", "path": None, "record_count": 0}
    data = read_json(path)
    if data.get("status") in {"missing", "unreadable"}:
        return {}, {"status": data.get("status"), "path": str(path), "record_count": 0, "error": data.get("error")}
    records = data.get("records") if isinstance(data.get("records"), list) else []
    statuses: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        check = record.get("check") if isinstance(record.get("check"), dict) else {}
        record_status = str(record.get("status") or "").strip()
        status = {
            "status": record_status,
            "guard_ok": (
                (record_status == "guard_ok" and check.get("ok") is True)
                or record_status in LIVE_FINANCIAL_CAPTURE_GUARD_OK_STATUSES
            ),
            "check_return_code": check.get("return_code"),
            "live_financials_length": record.get("live_financials_length"),
            "snapshot_path": record.get("snapshot_path") or record.get("next_action_file"),
            "financials_md": record.get("financials_md"),
            "property_name": record.get("property_name"),
            "lofty_property_id": record.get("lofty_property_id"),
            "live_distribution_verify": record.get("live_distribution_verify")
            if isinstance(record.get("live_distribution_verify"), dict)
            else {},
        }
        for key in (
            str(record.get("lofty_property_id") or "").strip(),
            str(record.get("financials_md") or "").strip(),
            str(record.get("property_name") or "").strip().lower(),
        ):
            if key:
                statuses[key] = status
    return statuses, {
        "status": data.get("status"),
        "path": str(path),
        "record_count": len(records),
        "guard_ok_count": sum(
            1
            for record in records
            if isinstance(record, dict)
            and (
                (
                    str(record.get("status") or "").strip() == "guard_ok"
                    and (record.get("check") if isinstance(record.get("check"), dict) else {}).get("ok") is True
                )
                or str(record.get("status") or "").strip() in LIVE_FINANCIAL_CAPTURE_GUARD_OK_STATUSES
            )
        ),
    }


def live_financial_status_for(prop: dict[str, Any], statuses: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    return (
        statuses.get(str(prop.get("lofty_property_id") or "").strip())
        or statuses.get(str(prop.get("financials_md") or "").strip())
        or statuses.get(str(prop.get("property_name") or "").strip().lower())
    )


def dry_run_live_patch_from_financial_status(live_status: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(live_status, dict):
        return {"utilities": 0.0}
    verify = live_status.get("live_distribution_verify")
    verify = verify if isinstance(verify, dict) else {}
    patch: dict[str, Any] = {"utilities": 0.0}
    denominator = parse_accounting_money(verify.get("cash_on_cash_denominator"))
    if denominator is not None and denominator > 0:
        patch["total_investment"] = denominator
    for source_key, target_key in (
        ("actual", "cash_flow"),
        ("actual_coc", "coc"),
        ("actual_current_loan", "current_loan"),
        ("actual_is_occupied", "is_occupied"),
        ("actual_monthly_loan_repayment", "monthly_loan_repayment"),
        ("actual_projected_rental_yield", "projected_rental_yield"),
    ):
        if source_key in verify:
            patch[target_key] = verify[source_key]
    return patch


def live_financial_capture_ready(live_status: dict[str, Any] | None) -> bool:
    if not isinstance(live_status, dict):
        return False
    if live_status.get("status") not in LIVE_FINANCIAL_CAPTURE_READY_STATUSES:
        return False
    try:
        live_length = int(live_status.get("live_financials_length") or 0)
    except (TypeError, ValueError):
        live_length = 0
    return live_length > 0 and bool(str(live_status.get("snapshot_path") or "").strip())


def live_financial_corrective_distribution_ready(
    live_status: dict[str, Any] | None,
    patch_payload: dict[str, Any],
) -> bool:
    if not isinstance(live_status, dict):
        return False
    if live_status.get("status") != "blocked_live_distribution_mismatch":
        return False
    if "cash_flow" not in patch_payload:
        return False
    verify = live_status.get("live_distribution_verify")
    if not isinstance(verify, dict) or verify.get("targeted") is not True:
        return False
    try:
        live_length = int(live_status.get("live_financials_length") or 0)
    except (TypeError, ValueError):
        live_length = 0
    return live_length > 0 and bool(str(live_status.get("snapshot_path") or "").strip())


def run_financial_publish(
    prop: dict[str, Any],
    runtime_map: Path,
    financial_patch_script: Path,
    payload_builder_script: Path,
    save_send_script: Path,
    apply: bool,
    close_extra_tabs: bool,
    run_month: str | None = None,
    live_financial_status: dict[str, Any] | None = None,
    distribution_guard_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    patch_command = [
        sys.executable,
        str(financial_patch_script),
        "--property",
        prop["lofty_property_id"],
        "--property-map",
        str(runtime_map),
        "--financials-only",
    ]
    patch_result = run_financial_patch(patch_command, run_month)
    result: dict[str, Any] = {
        "property_name": prop.get("property_name"),
        "lofty_property_id": prop.get("lofty_property_id"),
        "financials_md": prop.get("financials_md"),
        "ok": patch_result["ok"],
        "dry_run": not apply,
        "would_publish_financials": patch_result["ok"] and patch_result["field_count"] > 0,
        "patch_result": patch_result,
        "field_count": patch_result["field_count"],
        "fields": patch_result["fields"],
        "sources": patch_result["sources"],
        "live_financial_guard_status": (live_financial_status or {}).get("status"),
        "live_financial_guard_ok": (live_financial_status or {}).get("guard_ok") is True,
        "live_financial_capture_ready": live_financial_capture_ready(live_financial_status),
        "live_financial_snapshot_path": (live_financial_status or {}).get("snapshot_path"),
    }
    if not patch_result["ok"]:
        result["error"] = "financial patch build failed"
        return result
    if patch_result["field_count"] <= 0:
        result["ok"] = False
        result["error"] = "financial patch had no fields"
        return result
    patch_payload = patch_result["patch"].get("patch") if isinstance(patch_result.get("patch"), dict) else {}
    patch_payload = patch_payload if isinstance(patch_payload, dict) else {}
    unsafe_keys = sorted(key for key in patch_payload if key in UNSAFE_FINANCIAL_PATCH_KEYS)
    result["unsafe_patch_keys"] = unsafe_keys
    if unsafe_keys:
        result["ok"] = False
        result["would_publish_financials"] = False
        result["error"] = f"financial patch contains unsafe non-financial keys: {', '.join(unsafe_keys)}"
        return result
    guard_enabled = bool((distribution_guard_inputs or {}).get("packet_path"))
    corrective_distribution_ready = guard_enabled and live_financial_corrective_distribution_ready(
        live_financial_status,
        patch_payload,
    )
    result["live_financial_corrective_distribution_ready"] = corrective_distribution_ready
    result["live_distribution_verify"] = (
        live_financial_status.get("live_distribution_verify")
        if isinstance(live_financial_status, dict) and isinstance(live_financial_status.get("live_distribution_verify"), dict)
        else {}
    )
    if apply and not result["live_financial_capture_ready"] and not corrective_distribution_ready:
        result["ok"] = False
        result["would_publish_financials"] = False
        result["error"] = f"live financial capture not apply-ready: status={result['live_financial_guard_status'] or 'missing'}"
        return result

    financial_payload_files = [
        Path(prop["financial_save_payload_file"]),
        Path(prop["financial_send_payload_file"]),
    ]
    financial_payload_files_exist = all(path.is_file() for path in financial_payload_files)
    refresh_live_baseline = apply and guard_enabled
    if apply and (refresh_live_baseline or not financial_payload_files_exist):
        bootstrap_command = [
            sys.executable,
            str(payload_builder_script),
            "--property-id",
            prop["lofty_property_id"],
            "--property",
            prop["property_name"],
            "--get-manager-properties-payload-file",
            prop["get_manager_properties_payload_file"],
            "--save-payload-file",
            prop["financial_save_payload_file"],
            "--send-payload-file",
            prop["financial_send_payload_file"],
        ]
        if close_extra_tabs:
            bootstrap_command.append("--close-extra-tabs")
        timeout_seconds = int(os.environ.get("LOFTY_PM_COMMAND_TIMEOUT_SECONDS") or 120)
        bootstrap = subprocess.run(bootstrap_command, capture_output=True, text=True, timeout=timeout_seconds)
        result["bootstrap"] = {
            "command": bootstrap_command,
            "return_code": bootstrap.returncode,
            "ok": bootstrap.returncode == 0,
            "timeout_seconds": timeout_seconds,
            "stdout_tail": bootstrap.stdout[-4000:],
            "stderr_tail": bootstrap.stderr[-4000:],
            "fresh_live_baseline_required": refresh_live_baseline,
        }
        if bootstrap.returncode != 0:
            result["ok"] = False
            result["error"] = "financial payload bootstrap failed"
            return result
    elif not apply and not financial_payload_files_exist:
        result["bootstrap"] = {
            "ok": True,
            "skipped": True,
            "reason": "dry-run uses live financial capture status instead of browser payload bootstrap",
            "files": [str(path) for path in financial_payload_files],
            "fresh_live_baseline_required": False,
        }
    else:
        result["bootstrap"] = {
            "ok": True,
            "skipped": True,
            "reason": "existing financial payload files used for dry-run only",
            "files": [str(path) for path in financial_payload_files],
            "fresh_live_baseline_required": False,
        }

    if guard_enabled:
        if Path(prop["financial_save_payload_file"]).is_file():
            live_payload = read_json(Path(prop["financial_save_payload_file"]))
            live_patch = live_payload.get("patch") if isinstance(live_payload.get("patch"), dict) else {}
            result["live_patch_source"] = "financial_save_payload_file"
        else:
            live_patch = dry_run_live_patch_from_financial_status(live_financial_status)
            result["live_patch_source"] = "live_financial_capture_status_synthetic_dry_run"
        guarded_patch, distribution_guard = build_distribution_guard_patch(
            prop,
            patch_payload,
            live_patch,
            distribution_guard_inputs or {},
        )
        result["distribution_guard"] = distribution_guard
        if not guarded_patch:
            result["ok"] = False
            result["would_publish_financials"] = False
            result["error"] = f"distribution guard blocked: {distribution_guard.get('status') or 'unknown'}"
            return result
        patch_payload.update(guarded_patch)
    else:
        result["distribution_guard"] = {"status": "not_configured"}
    safe_patch_payload = {key: patch_payload[key] for key in sorted(patch_payload) if key in SAFE_LIVE_FINANCIAL_PATCH_KEYS}
    skipped_live_keys = sorted(key for key in patch_payload if key not in SAFE_LIVE_FINANCIAL_PATCH_KEYS)
    cash_flow_value = parse_accounting_money(safe_patch_payload.get("cash_flow"))
    projected_annual_cash_flow_value = parse_accounting_money(safe_patch_payload.get("projected_annual_cash_flow"))
    if (
        cash_flow_value is not None
        and projected_annual_cash_flow_value is not None
        and abs(cash_flow_value - projected_annual_cash_flow_value) > 0.01
    ):
        result["ok"] = False
        result["would_publish_financials"] = False
        result["error"] = (
            "financial patch annual cash_flow contract violation: "
            f"cash_flow={cash_flow_value} projected_annual_cash_flow={projected_annual_cash_flow_value}"
        )
        result["cash_flow_api_semantics"] = "Lofty property-owners API cash_flow is annualized; UI Current Month Distribution is cash_flow / 12"
        return result
    result["live_patch_keys"] = sorted(safe_patch_payload)
    result["live_field_count"] = len(safe_patch_payload)
    result["skipped_live_patch_keys"] = skipped_live_keys
    patch_file = Path(prop["financial_save_payload_file"]).with_suffix(".patch.json")
    patch_file.parent.mkdir(parents=True, exist_ok=True)
    patch_file.write_text(json.dumps(safe_patch_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["live_patch_file"] = str(patch_file)
    if not apply:
        return result
    if not safe_patch_payload:
        result["would_publish_financials"] = False
        result["skipped_reason"] = "no safe live financial fields"
        return result

    save_command = [
        sys.executable,
        str(save_send_script),
        "--get-manager-properties-payload-file",
        prop["get_manager_properties_payload_file"],
        "--save-payload-file",
        prop["financial_save_payload_file"],
        "--save-patch-file",
        str(patch_file),
        "--send-payload-file",
        prop["financial_send_payload_file"],
        "--property-id",
        prop["lofty_property_id"],
        "--skip-send",
    ]
    if close_extra_tabs:
        save_command.append("--close-extra-tabs")
    save_env = os.environ.copy()
    save_env.setdefault(SKIP_GET_PREFLIGHT_ENV, "1")
    timeout_seconds = int(os.environ.get("LOFTY_PM_COMMAND_TIMEOUT_SECONDS") or 120)
    saved = subprocess.run(save_command, capture_output=True, text=True, timeout=timeout_seconds, env=save_env)
    result["save"] = {
        "command": save_command,
        "return_code": saved.returncode,
        "ok": saved.returncode == 0,
        "timeout_seconds": timeout_seconds,
        "stdout_tail": saved.stdout[-4000:],
        "stderr_tail": saved.stderr[-4000:],
    }
    if saved.returncode != 0:
        result["ok"] = False
        result["error"] = "financial save failed"
    return result


def read_sent_state(path: Path | None) -> str | None:
    if not path or not path.exists():
        return None
    return path.read_text(encoding="utf-8", errors="replace").strip() or None


def write_sent_state(path: Path, run_month: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(run_month + "\n", encoding="utf-8")
    tmp.replace(path)


def send_lock_path(sent_state_file: Path | None) -> Path | None:
    if not sent_state_file:
        return None
    return sent_state_file.with_suffix(sent_state_file.suffix + ".in-progress.json")


def read_send_lock(path: Path | None) -> dict[str, Any] | None:
    if not path or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"status": "unreadable", "path": str(path), "error": str(exc)}
    return data if isinstance(data, dict) else {"status": "unreadable", "path": str(path), "error": "not a JSON object"}


def write_send_lock(path: Path, run_month: str, property_count: int, send_decision_digest: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": "in_progress",
        "run_month": run_month,
        "property_count": property_count,
        "send_decision_digest": send_decision_digest,
        "created_at": iso_z(),
        "purpose": "Pre-send lock for monthly owner email. Remove only after verifying no owner emails were sent or after monthly sent state is written.",
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def clear_send_lock(path: Path | None) -> None:
    if path and path.exists():
        path.unlink()


def update_send_lock(path: Path | None, updates: dict[str, Any]) -> None:
    if not path or not path.exists():
        return
    current = read_send_lock(path)
    payload = current if isinstance(current, dict) else {}
    payload = {**payload, **updates, "updated_at": iso_z()}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Publish guarded monthly Lofty UPDATES.md entries to Lofty PM. "
            "Direct live financial corrections require a separate explicit gate. "
            "Native Lofty owner email is disabled; owner email must use the reviewed non-native packet flow."
        )
    )
    parser.add_argument("--index-csv", required=True, type=Path)
    parser.add_argument("--guarded-apply-report", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--runtime-map", required=True, type=Path)
    parser.add_argument("--payload-dir", required=True, type=Path)
    parser.add_argument("--portfolio-map", type=Path)
    parser.add_argument("--skill-map", type=Path)
    parser.add_argument("--publish-script", required=True, type=Path)
    parser.add_argument("--financial-patch-script", type=Path)
    parser.add_argument(
        "--live-financial-corrective",
        action="store_true",
        help="Explicitly authorize corrective writes to the currently live listing financial fields",
    )
    parser.add_argument("--live-financial-capture-report", type=Path)
    parser.add_argument("--payload-builder-script", type=Path)
    parser.add_argument("--save-send-script", type=Path)
    parser.add_argument("--monthly-readiness-report", type=Path)
    parser.add_argument("--review-candidate-packet-report", type=Path)
    parser.add_argument("--transfer-reconciliation-report", type=Path)
    parser.add_argument("--rent-roll-source-report", type=Path)
    parser.add_argument("--rent-roll-occupancy-summary-csv", type=Path)
    parser.add_argument("--lofty-live-properties-report", type=Path)
    parser.add_argument("--yhome-transition-csv", type=Path)
    parser.add_argument("--listing-update-policy", type=Path, default=DEFAULT_LISTING_UPDATE_POLICY)
    parser.add_argument("--exclude-property", action="append", default=[])
    parser.add_argument(
        "--property",
        action="append",
        default=[],
        help="Restrict this run to exact property names or Lofty property IDs.",
    )
    parser.add_argument("--guild-test-post-report", type=Path)
    parser.add_argument("--require-guild-test-post-before-email", action="store_true")
    parser.add_argument("--send-interval-days", type=int, default=31)
    parser.add_argument("--run-month", default=datetime.now(timezone.utc).strftime("%Y-%m"))
    parser.add_argument("--sent-state-file", type=Path)
    parser.add_argument("--owner-email-blocked-reason", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--send-owner-emails", action="store_true")
    parser.add_argument("--email-only", action="store_true", help="Send validated current updates without rewriting listing or financial fields")
    parser.add_argument("--financial-only", action="store_true", help="Apply or preview guarded financial fields without publishing UPDATES.md or sending owner email")
    parser.add_argument("--close-extra-tabs", action="store_true")
    parser.add_argument("--no-bootstrap-missing-financials-md", action="store_true")
    args = parser.parse_args(argv)

    issues: list[str] = []
    if not args.index_csv.is_file():
        issues.append(f"monthly index missing: {args.index_csv}")
    if args.email_only and not args.send_owner_emails:
        issues.append("--email-only requires --send-owner-emails")
    if not args.publish_script.is_file():
        issues.append(f"publish script missing: {args.publish_script}")
    financial_publish_enabled = (
        args.live_financial_corrective
        and args.financial_patch_script is not None
        and not args.email_only
    )
    payload_builder_script = args.payload_builder_script or (args.publish_script.parent / "build_lofty_pm_payloads.py")
    save_send_script = args.save_send_script or (args.publish_script.parent / "save_and_send_lofty_pm_update.py")
    if args.email_only and args.financial_only:
        issues.append("--email-only conflicts with --financial-only")
    if args.financial_only and not financial_publish_enabled:
        issues.append("--financial-only requires --live-financial-corrective and --financial-patch-script")
    if args.financial_patch_script is not None and not args.live_financial_corrective:
        issues.append("--financial-patch-script requires --live-financial-corrective")
    if args.financial_only and args.send_owner_emails:
        issues.append("--financial-only refuses --send-owner-emails")
    if args.email_only and not save_send_script.is_file():
        issues.append(f"email-only save/send script missing: {save_send_script}")
    if financial_publish_enabled:
        if not args.financial_patch_script or not args.financial_patch_script.is_file():
            issues.append(f"financial patch script missing: {args.financial_patch_script}")
        if not payload_builder_script.is_file():
            issues.append(f"payload builder script missing: {payload_builder_script}")
        if not save_send_script.is_file():
            issues.append(f"save/send script missing: {save_send_script}")
    live_financial_guard_statuses, live_financial_guard_report = live_financial_statuses(args.live_financial_capture_report)
    sent_state_month = read_sent_state(args.sent_state_file)
    lock_path = send_lock_path(args.sent_state_file)
    existing_send_lock = read_send_lock(lock_path)
    send_lock_status = "not_requested"
    readiness = readiness_snapshot(args.monthly_readiness_report)
    guild_test_post = guild_test_post_snapshot(args.guild_test_post_report, args.run_month)
    readiness_blocked_reason = None
    if readiness.get("status") not in {None, "not_configured"} and readiness.get("owner_email_allowed") is not True:
        readiness_blocked_reason = monthly_readiness_blocked_reason(readiness)
    send_blocked_reason = args.owner_email_blocked_reason.strip() or readiness_blocked_reason
    if (
        args.send_owner_emails
        and args.require_guild_test_post_before_email
        and not send_blocked_reason
        and guild_test_post.get("valid") is not True
    ):
        send_blocked_reason = "monthly owner email blocked until a matching Lofty guild property-channel test post report is valid"
    native_owner_email_override_env_enabled = os.environ.get(NATIVE_OWNER_EMAIL_OVERRIDE_ENV) == "1"
    native_owner_email_safe_env_enabled = os.environ.get(NATIVE_OWNER_EMAIL_SAFE_ENV) == "1"
    native_owner_email_allowed = bool(native_owner_email_safe_env_enabled)
    readiness_blocked_reason_matches = (
        readiness_blocked_reason is None
        or send_blocked_reason == readiness_blocked_reason
    )
    if args.send_owner_emails and not args.sent_state_file:
        issues.append("--sent-state-file is required when --send-owner-emails is set")
    elif args.send_owner_emails and sent_state_month == args.run_month:
        send_blocked_reason = f"owner emails already sent for {args.run_month}"
    guarded_apply = read_json(args.guarded_apply_report)
    apply_ready, apply_reason = guarded_apply_ready_for_publish_mode(
        guarded_apply,
        False if args.financial_only else args.apply,
    )
    raw_guarded_apply_issues = guarded_apply_issue_details(guarded_apply)
    guarded_apply_issues, suppressed_guarded_apply_issues = suppress_deferred_guarded_apply_issues(
        raw_guarded_apply_issues,
        readiness,
    )
    guarded_apply_blockers = guarded_apply_actionable_blockers(guarded_apply)
    if not apply_ready:
        issues.append(apply_reason)
        issues.extend(f"guarded_apply:{issue}" for issue in guarded_apply_issues[:10])

    rows = load_index(args.index_csv) if args.index_csv.is_file() else []
    effective_skill_map = effective_skill_map_path(args.skill_map)
    candidates = property_id_candidates(args.portfolio_map, effective_skill_map)
    yhome_exclusions, yhome_guard = load_yhome_transition_exclusions(args.yhome_transition_csv)
    if args.yhome_transition_csv and yhome_guard.get("status") != "ok":
        issues.append(f"Yhome Transition Reconciliation guard unavailable: {yhome_guard.get('status')}: {args.yhome_transition_csv}")
    policy_exclusions, _ = listing_update_policy_exclusion_records(args.listing_update_policy)
    manual_exclusions = manual_exclusion_records([*DEFAULT_MANUAL_EXCLUDED_PROPERTIES, *args.exclude_property])
    financial_holds = financial_hold_records(args.transfer_reconciliation_report)
    guarded_apply_exclusions = guarded_apply_exclusion_records(guarded_apply)
    excluded_property_guards = [*yhome_exclusions, *policy_exclusions, *manual_exclusions, *financial_holds]
    properties, records = build_runtime_map(
        rows,
        candidates,
        args.payload_dir,
        args.run_month,
        not args.no_bootstrap_missing_financials_md,
        excluded_property_guards,
    )
    if args.property:
        requested = {normalize(value) for value in args.property if normalize(value)}
        selected = [
            prop
            for prop in properties
            if normalize(str(prop.get("property_name") or "")) in requested
            or normalize(str(prop.get("lofty_property_id") or "")) in requested
        ]
        selected_keys = {
            normalize(str(prop.get("property_name") or ""))
            for prop in selected
        } | {
            normalize(str(prop.get("lofty_property_id") or ""))
            for prop in selected
        }
        missing_requested = sorted(requested - selected_keys)
        if missing_requested:
            issues.append(f"requested property not in active publish scope: {', '.join(missing_requested)}")
        properties = selected
        records = [
            record
            for record in records
            if normalize(str(record.get("property_name") or "")) in selected_keys
            or normalize(str(record.get("lofty_property_id") or "")) in selected_keys
        ]
    append_unmapped_exclusion_records(records, guarded_apply_exclusions, candidates, args.payload_dir)
    map_issues = [record for record in records if record.get("status", "").startswith("blocked_")]
    excluded_records = [record for record in records if record.get("status") == "excluded_no_live_update_or_email"]
    issues.extend(f"{record['status']}: {record['property_name']}" for record in map_issues[:20])
    description_report = description_check_report(
        properties,
        args.rent_roll_source_report,
        args.rent_roll_occupancy_summary_csv,
        args.lofty_live_properties_report,
        args.run_month,
    )
    if description_report.get("status") not in {"ok", "not_configured"}:
        source_issues = description_report.get("source_issues") or []
        if source_issues:
            issues.extend(f"description_check:{issue}" for issue in source_issues[:10])
        for record in description_report.get("records", []):
            if isinstance(record, dict) and record.get("status") in DESCRIPTION_CHECK_BLOCKING_STATUSES:
                issues.append(
                    "description_check:"
                    f"{record.get('status')}:{record.get('property_name')}:"
                    f"{','.join(str(issue) for issue in (record.get('issues') or []))}"
                )

    effective_runtime_map, runtime_map_scope_issue, runtime_map_expected_portfolio_count = runtime_map_scope_guard(
        args.runtime_map,
        len(properties),
        args.review_candidate_packet_report,
        args.live_financial_capture_report,
    )
    if runtime_map_scope_issue:
        issues.append(runtime_map_scope_issue)
    effective_runtime_map.parent.mkdir(parents=True, exist_ok=True)
    effective_runtime_map.write_text(json.dumps({"properties": properties, "records": records}, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    publish_results: list[dict[str, Any]] = []
    financial_publish_results: list[dict[str, Any]] = []
    approved_update_sources = load_review_candidate_sources(args.review_candidate_packet_report, args.run_month)
    distribution_guard_inputs = load_distribution_guard_inputs(
        args.review_candidate_packet_report,
        args.yhome_transition_csv,
        args.report,
    )
    send_decision_inputs = {
        "run_month": args.run_month,
        "requested": args.send_owner_emails,
        "send_interval_days": args.send_interval_days,
        "sent_state_file": str(args.sent_state_file) if args.sent_state_file else None,
        "sent_state_month": sent_state_month,
        "readiness_digest": readiness.get("digest"),
        "readiness_owner_email_allowed": readiness.get("owner_email_allowed"),
        "guild_test_post_required": args.require_guild_test_post_before_email,
        "guild_test_post_valid": guild_test_post.get("valid"),
        "guild_test_post_digest": guild_test_post.get("digest"),
        "native_owner_email_allowed": native_owner_email_allowed,
        "native_owner_email_override_env": NATIVE_OWNER_EMAIL_OVERRIDE_ENV,
        "native_owner_email_override_env_enabled": native_owner_email_override_env_enabled,
        "native_owner_email_safe_env": NATIVE_OWNER_EMAIL_SAFE_ENV,
        "native_owner_email_safe_env_enabled": native_owner_email_safe_env_enabled,
        "guarded_apply_status": guarded_apply.get("status"),
        "guarded_apply_apply": guarded_apply.get("apply"),
        "property_count": len(properties),
        "excluded_property_count": len(excluded_records),
        "excluded_property_names": [record.get("property_name") for record in excluded_records],
        "properties": [
            {
                "property_name": prop.get("property_name"),
                "lofty_property_id": prop.get("lofty_property_id"),
                "updates_md": prop.get("updates_md"),
                "financials_md": prop.get("financials_md"),
            }
            for prop in properties
        ],
    }
    send_decision_digest = stable_digest(send_decision_inputs)
    existing_send_lock_unreadable = bool(
        isinstance(existing_send_lock, dict) and existing_send_lock.get("status") == "unreadable"
    )
    existing_lock_same_month = bool(existing_send_lock and existing_send_lock.get("run_month") == args.run_month)
    existing_lock_decision_digest = (
        str(existing_send_lock.get("send_decision_digest") or "") if isinstance(existing_send_lock, dict) else ""
    )
    existing_lock_matches_send_decision = (
        existing_lock_same_month
        and bool(existing_lock_decision_digest)
        and existing_lock_decision_digest == send_decision_digest
    )
    if args.send_owner_emails and not send_blocked_reason and existing_send_lock_unreadable:
        send_blocked_reason = "owner email send lock is unreadable; manual review required before send"
        send_lock_status = "blocked_unreadable_lock"
    elif args.send_owner_emails and not send_blocked_reason and existing_lock_same_month:
        send_blocked_reason = f"owner email send lock exists for {args.run_month}; manual review required before resend"
        send_lock_status = "blocked_existing_lock"
    elif args.send_owner_emails and not send_blocked_reason and not native_owner_email_allowed:
        send_blocked_reason = NATIVE_OWNER_EMAIL_DISABLED_REASON
    effective_send_owner_emails = args.send_owner_emails and not send_blocked_reason
    if args.apply and effective_send_owner_emails and not issues and lock_path:
        write_send_lock(lock_path, args.run_month, len(properties), send_decision_digest)
        send_lock_status = "written"
    if financial_publish_enabled and (not issues or not args.apply):
        for prop in properties:
            financial_publish_results.append(
                run_financial_publish(
                    prop,
                    effective_runtime_map,
                    args.financial_patch_script,
                    payload_builder_script,
                    save_send_script,
                    args.apply,
                    args.close_extra_tabs,
                    args.run_month,
                    live_financial_status_for(prop, live_financial_guard_statuses),
                    distribution_guard_inputs,
                )
            )
        for result in financial_publish_results:
            if result.get("ok") is not True:
                issues.append(f"financial publish failed: {result.get('property_name')}: {result.get('error') or 'unknown error'}")

    if args.apply and not issues and not args.financial_only:
        for prop in properties:
            approved_update_source = approved_update_source_for(prop, approved_update_sources)
            should_require_approved_update = bool(args.review_candidate_packet_report and args.review_candidate_packet_report.is_file())
            if should_require_approved_update and not approved_update_source:
                issues.append(f"missing approved current update source: {prop.get('property_name') or prop.get('updates_md')}")
                continue
            command = [
                sys.executable,
                str(args.publish_script),
                "--property",
                prop["updates_md"],
                "--map-file",
                str(effective_runtime_map),
                "--send-interval-days",
                str(args.send_interval_days),
                "--send-current-on-first-run",
                "--save-payload-file",
                prop["save_payload_file"],
                "--send-payload-file",
                prop["send_payload_file"],
                "--wrapper-script",
                str(save_send_script),
            ]
            if args.email_only:
                command.append("--email-only")
            if approved_update_source:
                command.extend(["--approved-update-source", str(approved_update_source)])
            if args.review_candidate_packet_report and args.review_candidate_packet_report.is_file():
                command.extend(
                    [
                        "--review-candidate-packet-report",
                        str(args.review_candidate_packet_report),
                        "--run-month",
                        args.run_month,
                        "--require-monthly-financial-summary",
                    ]
                )
            if not effective_send_owner_emails:
                command.append("--skip-send")
            if args.close_extra_tabs:
                command.append("--close-extra-tabs")
            result = run_publish(command)
            if should_require_approved_update:
                result["approved_update_source"] = str(approved_update_source)
            publish_results.append(result)
    elif not args.apply and not issues and not args.financial_only:
        for prop in properties:
            approved_update_source = approved_update_source_for(prop, approved_update_sources)
            publish_results.append(
                {
                    "property_name": prop["property_name"],
                    "updates_md": prop["updates_md"],
                    "lofty_property_id": prop["lofty_property_id"],
                    "ok": True,
                    "dry_run": True,
                    "would_publish": True,
                    "would_send_owner_email": effective_send_owner_emails,
                    "will_send": effective_send_owner_emails,
                    "skip_send": not effective_send_owner_emails,
                    "send_interval_days": args.send_interval_days,
                    "listing_update_scope": "full_history",
                    "approved_update_source": str(approved_update_source or ""),
                    "listing_update_guard_ok": True,
                    "listing_update_char_count": None,
                    "listing_update_line_count": None,
                    "owner_email_send_evidence": False,
                }
            )

    failed_publish = [result for result in publish_results if result.get("ok") is not True]
    failed_financial_publish = [result for result in financial_publish_results if result.get("ok") is not True]
    distribution_guard_results = [
        result.get("distribution_guard")
        for result in financial_publish_results
        if isinstance(result.get("distribution_guard"), dict)
    ]
    dry_run_would_send_count = sum(1 for result in publish_results if result.get("would_send_owner_email") is True)
    owner_email_will_send_count = sum(1 for result in publish_results if result.get("will_send") is True)
    owner_email_skipped_count = sum(1 for result in publish_results if result.get("skip_send") is True or result.get("will_send") is False)
    owner_email_send_evidence_count = sum(1 for result in publish_results if result.get("owner_email_send_evidence") is True)
    owner_email_send_evidence_issue_count = sum(len(result.get("owner_email_send_evidence_issues") or []) for result in publish_results)
    owner_email_sent_or_would_send_count = owner_email_send_evidence_count + dry_run_would_send_count
    listing_update_guard_issue_count = sum(1 for result in publish_results if result.get("listing_update_guard_ok") is not True)
    listing_update_non_history_count = sum(1 for result in publish_results if result.get("listing_update_scope") != "full_history")
    listing_update_full_history_count = sum(1 for result in publish_results if result.get("listing_update_scope") == "full_history")
    if listing_update_guard_issue_count:
        issues.append(f"listing_update_guard_issue_count={listing_update_guard_issue_count}")
    if listing_update_non_history_count:
        issues.append(f"listing_update_non_history_count={listing_update_non_history_count}")
    excluded_payload_file_count = sum(
        1
        for record in excluded_records
        for field in ("save_payload_file", "send_payload_file", "financial_save_payload_file", "financial_send_payload_file")
        if record.get(field)
    )
    removed_excluded_payload_file_count = sum(
        len(record.get("removed_excluded_payload_files") or [])
        for record in excluded_records
        if isinstance(record.get("removed_excluded_payload_files"), list)
    )
    excluded_owner_email_candidate_count = sum(
        1
        for record in excluded_records
        if record.get("will_send") is True
        or record.get("would_send_owner_email") is True
        or record.get("owner_email_send_evidence") is True
    )
    sent_state_write_status = "not_requested"
    owner_email_idempotency = {
        "configured": args.sent_state_file is not None,
        "max_send_per_month": True,
        "run_month": args.run_month,
        "sent_state_file": str(args.sent_state_file) if args.sent_state_file else None,
        "sent_state_month": sent_state_month,
        "send_lock_file": str(lock_path) if lock_path else None,
        "send_lock_status": send_lock_status,
        "send_decision_digest": send_decision_digest,
        "existing_send_lock_decision_digest": existing_lock_decision_digest or None,
        "existing_send_lock_matches_send_decision": existing_lock_matches_send_decision,
        "send_blocked_reason": send_blocked_reason,
        "safe_to_send_now": bool(args.send_owner_emails and not send_blocked_reason and args.sent_state_file and sent_state_month != args.run_month),
        "policy": "Owner email can send at most once per run_month; pre-send lock blocks concurrent/retry sends; sent-state write requires complete send evidence.",
    }
    if args.apply and effective_send_owner_emails and not failed_publish and not failed_financial_publish:
        if owner_email_will_send_count <= 0:
            sent_state_write_status = "skipped_no_sends_needed"
            clear_send_lock(lock_path)
            if send_lock_status == "written":
                send_lock_status = "cleared_no_sends_needed"
        elif owner_email_send_evidence_count == owner_email_will_send_count:
            if not args.sent_state_file:
                issues.append("cannot write monthly sent state: missing --sent-state-file")
                sent_state_write_status = "failed_missing_state_file"
            else:
                write_sent_state(args.sent_state_file, args.run_month)
                clear_send_lock(lock_path)
                send_lock_status = "cleared_after_sent_state"
                sent_state_write_status = "written"
                sent_state_month = args.run_month
                owner_email_idempotency["sent_state_month"] = sent_state_month
        else:
            issues.append(f"owner email send evidence mismatch ({owner_email_send_evidence_count}/{owner_email_will_send_count}); evidence_issues={owner_email_send_evidence_issue_count}")
            if send_lock_status == "written":
                send_lock_status = "left_for_review_after_evidence_mismatch"
            sent_state_write_status = "blocked_evidence_mismatch"
    elif args.apply and effective_send_owner_emails and failed_publish and send_lock_status == "written":
        send_lock_status = "left_for_review_after_publish_failure"
    elif args.apply and effective_send_owner_emails and failed_financial_publish and send_lock_status == "written":
        send_lock_status = "left_for_review_after_financial_publish_failure"
    elif args.apply and effective_send_owner_emails and not failed_publish and owner_email_will_send_count <= 0 and lock_path:
        clear_send_lock(lock_path)
        send_lock_status = "cleared_no_sends_needed"
    status = "failed" if failed_publish or failed_financial_publish else "review" if issues else "ok"
    publish_blocker = publish_primary_blocker(
        issues,
        guarded_apply_blockers,
        guarded_apply_issues,
        readiness,
        send_blocked_reason,
        include_send_blocker=False,
    )
    email_primary_blocker = publish_primary_blocker(
        issues,
        guarded_apply_blockers,
        guarded_apply_issues,
        readiness,
        send_blocked_reason,
    )
    primary_blocker = publish_blocker
    owner_email_idempotency["send_lock_status"] = send_lock_status
    owner_email_idempotency["sent_state_write_status"] = sent_state_write_status
    owner_email_idempotency["send_blocked_reason"] = send_blocked_reason
    owner_email_idempotency["safe_to_send_now"] = bool(
        args.send_owner_emails
        and effective_send_owner_emails
        and args.sent_state_file
        and sent_state_month != args.run_month
        and send_lock_status
        not in {
            "blocked_existing_lock",
            "blocked_unreadable_lock",
            "left_for_review_after_evidence_mismatch",
            "left_for_review_after_publish_failure",
            "left_for_review_after_financial_publish_failure",
        }
    )
    owner_email_send_decision = {
        "requested": args.send_owner_emails,
        "effective": effective_send_owner_emails,
        "blocked_reason": send_blocked_reason,
        "native_owner_email_allowed": native_owner_email_allowed,
        "native_owner_email_override_env": NATIVE_OWNER_EMAIL_OVERRIDE_ENV,
        "native_owner_email_override_env_enabled": native_owner_email_override_env_enabled,
        "native_owner_email_safe_env": NATIVE_OWNER_EMAIL_SAFE_ENV,
        "native_owner_email_safe_env_enabled": native_owner_email_safe_env_enabled,
        "safe_to_send_now": owner_email_idempotency["safe_to_send_now"],
        "sent_state_file": str(args.sent_state_file) if args.sent_state_file else None,
        "sent_state_month": sent_state_month,
        "send_lock_file": str(lock_path) if lock_path else None,
        "send_lock_status": send_lock_status,
        "send_decision_digest": send_decision_digest,
        "existing_send_lock_decision_digest": existing_lock_decision_digest or None,
        "existing_send_lock_matches_send_decision": existing_lock_matches_send_decision,
        "sent_state_write_status": sent_state_write_status,
        "will_send_count": owner_email_will_send_count,
        "send_evidence_count": owner_email_send_evidence_count,
        "send_evidence_issue_count": owner_email_send_evidence_issue_count,
    }
    if args.apply and lock_path and lock_path.exists() and not existing_send_lock_unreadable:
        update_send_lock(
            lock_path,
            {
                "status": send_lock_status,
                "run_month": args.run_month,
                "property_count": len(properties),
                "send_decision_digest": send_decision_digest,
                "sent_state_write_status": sent_state_write_status,
                "send_blocked_reason": send_blocked_reason,

                "owner_email_send_intended_count": len(properties) if effective_send_owner_emails else 0,
                "owner_email_send_evidence_count": owner_email_send_evidence_count, "owner_email_send_evidence_issue_count": owner_email_send_evidence_issue_count,

                "publish_result_count": len(publish_results),

                "financial_publish_result_count": len(financial_publish_results),
                "financial_publish_failed_count": len(failed_financial_publish),
                "owner_email_send_attempted": owner_email_will_send_count > 0,
                "owner_email_send_proven_complete": (
                    owner_email_will_send_count > 0
                    and owner_email_send_evidence_count == owner_email_will_send_count
                    and owner_email_send_evidence_issue_count == 0
                ),
                "safe_retry_without_duplicate_owner_email": (
                    effective_send_owner_emails
                    and len(properties) > 0
                    and owner_email_send_evidence_count == 0
                    and len(publish_results) == 0
                    and len(failed_financial_publish) > 0
                ),
                "manual_review_required": send_lock_status
                in {
                    "left_for_review_after_evidence_mismatch",
                    "left_for_review_after_publish_failure",
                    "left_for_review_after_financial_publish_failure",
                },
                "manual_review_reason": (
                    "owner_email_send_evidence_mismatch"
                    if send_lock_status == "left_for_review_after_evidence_mismatch"
                    else "publish_failure_before_sent_state"
                    if send_lock_status
                    in {
                        "left_for_review_after_publish_failure",
                        "left_for_review_after_financial_publish_failure",
                    }
                    else None
                ),
            },
        )
    report = {
        "generated_at": iso_z(),
        "status": status,
        "apply": args.apply,
        "send_owner_emails": args.send_owner_emails,
        "email_only": args.email_only,
        "financial_only": args.financial_only,
        "effective_send_owner_emails": effective_send_owner_emails,
        "run_month": args.run_month,
        "sent_state_file": str(args.sent_state_file) if args.sent_state_file else None,
        "sent_state_month": sent_state_month,
        "send_lock_file": str(lock_path) if lock_path else None,
        "send_lock_status": send_lock_status,
        "existing_send_lock": existing_send_lock,
        "send_decision_inputs": send_decision_inputs,
        "send_decision_digest": send_decision_digest,
        "existing_send_lock_decision_digest": existing_lock_decision_digest or None,
        "existing_send_lock_matches_send_decision": existing_lock_matches_send_decision,
        "sent_state_write_status": sent_state_write_status,
        "send_blocked_reason": send_blocked_reason,
        "email_primary_blocker": email_primary_blocker,
        "discord_review_handoff_ready": bool(not publish_blocker and len(properties) > 0 and len(failed_publish) == 0 and len(failed_financial_publish) == 0),
        "discord_review_handoff_policy": "Discord review may proceed when publish payloads are clean; owner email remains final gated send.",
        "send_interval_days": args.send_interval_days,
        "owner_email_policy": "max once per run_month; requires readiness, guarded apply, SEND_OWNER_EMAILS=1, and no same-month sent/lock state",
        "native_owner_email_disabled_reason": NATIVE_OWNER_EMAIL_DISABLED_REASON,
        "native_owner_email_override_env": NATIVE_OWNER_EMAIL_OVERRIDE_ENV,
        "native_owner_email_override_env_enabled": native_owner_email_override_env_enabled,
        "native_owner_email_safe_env": NATIVE_OWNER_EMAIL_SAFE_ENV,
        "native_owner_email_safe_env_enabled": native_owner_email_safe_env_enabled,
        "live_financial_guard_report": live_financial_guard_report,
        "live_financial_guard_required_for_apply": financial_publish_enabled,
        "review_candidate_packet_report": str(args.review_candidate_packet_report) if args.review_candidate_packet_report else None,
        "listing_update_requires_monthly_financial_summary": bool(
            args.review_candidate_packet_report and args.review_candidate_packet_report.is_file()
        ),
        "description_check_report": description_report,
        "description_check_status": description_report.get("status"),
        "description_check_blocking_count": description_report.get("blocking_count"),
        "description_check_source_issue_count": description_report.get("source_issue_count"),
        "description_check_policy": "Active DESCRIPTION.md must match rent-roll or Lofty-live before owner email.",
        "owner_email_idempotency": owner_email_idempotency,
        "owner_email_send_decision": owner_email_send_decision,
        "monthly_readiness_snapshot": readiness,
        "monthly_readiness_blocked_reason": readiness_blocked_reason,
        "monthly_readiness_blocked_reason_matches": readiness_blocked_reason_matches,
        "guild_test_post_required_before_email": args.require_guild_test_post_before_email,
        "guild_test_post_snapshot": guild_test_post,
        "guarded_apply_status": guarded_apply.get("status"),
        "guarded_apply_apply": guarded_apply.get("apply"),
        "guarded_apply_live_ready": guarded_apply_live_ready(guarded_apply),
        "guarded_apply_publish_mode_ready": apply_ready,
        "guarded_apply_publish_mode_reason": apply_reason,
        "guarded_apply_raw_issues": raw_guarded_apply_issues,
        "guarded_apply_issues": guarded_apply_issues,
        "guarded_apply_suppressed_issues": suppressed_guarded_apply_issues,
        "guarded_apply_suppressed_issue_count": len(suppressed_guarded_apply_issues),
        "guarded_apply_issue_suppression_policy": "Suppressed issue lines remain blocked until guarded apply is ok.",
        "guarded_apply_blocker_count": len(guarded_apply_blockers),
        "guarded_apply_actionable_blockers": guarded_apply_blockers,
        "yhome_transition_guard": yhome_guard,
        "manual_excluded_property_names": [row["property_name"] for row in manual_exclusions],
        "financial_hold_property_count": len(financial_holds),
        "financial_hold_property_names": [row["property_name"] for row in financial_holds],
        "financial_publish_enabled": financial_publish_enabled,
        "issues": issues,
        "issue_count": len(issues),
        "primary_blocker": primary_blocker,
        "actionable_summary": {"primary_blocker": primary_blocker, "actionable_blocker_count": 1 if primary_blocker else 0, "email_primary_blocker": email_primary_blocker, "email_actionable_blocker_count": 1 if email_primary_blocker else 0, "guarded_apply_blocker_count": len(guarded_apply_blockers), "guarded_apply_issue_count": len(guarded_apply_issues), "publish_issue_count": len(issues),"send_blocked": bool(send_blocked_reason), "safe_external_effect": bool(args.apply and not issues)},
        "runtime_map": str(args.runtime_map),
        "effective_runtime_map": str(effective_runtime_map),
        "skill_map": str(args.skill_map) if args.skill_map else None,
        "effective_skill_map": str(effective_skill_map) if effective_skill_map else None,
        "effective_skill_map_defaulted": bool(effective_skill_map and effective_skill_map != args.skill_map),
        "runtime_map_scope_issue": runtime_map_scope_issue,
        "runtime_map_expected_portfolio_count": runtime_map_expected_portfolio_count,
        "records": records,
        "property_count": len(properties),
        "excluded_property_count": len(excluded_records), "excluded_property_names": [r.get("property_name") for r in excluded_records],
        "excluded_payload_file_count": excluded_payload_file_count, "removed_excluded_payload_file_count": removed_excluded_payload_file_count, "excluded_owner_email_candidate_count": excluded_owner_email_candidate_count,
        "active_property_only_policy": "Yhome Transition Reconciliation column-B, listing/manual exclusions are excluded from Lofty PM publish payloads and owner-email candidates",
        "publish_attempted": bool(args.apply and not args.financial_only and not publish_blocker), "publish_result_count": len(publish_results), "updates_publish_result_count": len(publish_results), "publish_results": publish_results,
        "financial_publish_result_count": len(financial_publish_results), "financial_publish_failed_count": len(failed_financial_publish), "financial_publish_field_count": sum(len(r.get("fields") or []) for r in financial_publish_results), "financial_publish_results": financial_publish_results,
        "financials_md_bootstrapped_count": sum(1 for r in records if r.get("financials_md_bootstrapped") is True),
        "owner_email_send_evidence_count": owner_email_send_evidence_count, "owner_email_send_evidence_issue_count": owner_email_send_evidence_issue_count,
        "listing_update_guard_issue_count": listing_update_guard_issue_count, "listing_update_full_history_count": listing_update_full_history_count, "listing_update_non_history_count": listing_update_non_history_count, "listing_update_policy": "full UPDATES.md history; owner emails current only",
    }
    args.report.parent.mkdir(parents=True, exist_ok=True); args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 1 if status == "failed" else 2 if status == "review" else 0


if __name__ == "__main__":
    raise SystemExit(main())
