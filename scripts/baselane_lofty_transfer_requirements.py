#!/usr/bin/env python3
"""Report Lofty transfer amounts from verified spendable DAO cash.

The transfer rule is intentionally conservative:
- ECO Operating Cash/ECO Net DAO Funds is verified unrestricted cash after
  recorded obligations and restrictions.
- Mapped live Baselane bank cash is separate custody reconciliation evidence.
- Cash settlement basis is reported separately for transfer review and excludes
  non-cash closes and unsettled accrual journals.
- ECO General Ledger is the full per-property ECO GL Column E/Amount sum.
- Co-ownership properties must retain at least ``--eco-minimum`` across
  ECO-held spendable cash plus positive Lofty Operating Reserve.
- Amount "sendable to Lofty" is the combined surplus above that floor, capped
  by the non-negative cash actually held by ECO.
- If source cleanup or CF reflection is not clean, exact send amounts are held.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from canonical_property_ledger import DivergentCanonicalLedgerError, resolve_equivalent_ledgers
from coownership_mortgage_policy import NO_DAO_MORTGAGE_PROPERTY_KEYS, is_no_dao_mortgage_property
from baselane_reconciliation_policy import is_cash_basis_excluded_row
from coownership_reserve_policy import (
    LOCAL_FINANCIALS_ONLY_PROPERTIES,
    canonical_property as canonical_reserve_property,
    combined_reserve_position,
)
from lofty_monthly_exclusions import monthly_exclusion_guards


ROOT = Path(__file__).absolute().parents[1]
DEFAULT_CANDIDATE_PACKET = ROOT / "reports/baselane_financials_monthly_review_candidate_packet.json"
DEFAULT_CF_BALANCE_REPORT = ROOT / "reports/baselane_cf_balance_sheet_consistency_audit.json"
DEFAULT_SOURCE_CLEANUP_QUEUE = ROOT / "reports/baselane_source_cleanup_queue.json"
DEFAULT_SOURCE_CASH_REPORT = ROOT / "reports/baselane_daily_source_cash_balance_report.json"
DEFAULT_SOURCE_CASH_RECONCILIATION_ACTIONS = ROOT / "reports/baselane_source_cash_reconciliation_actions.json"
DEFAULT_ECOGL_AUTONOMY_REPORT = ROOT / "reports/baselane_ecogl_data_quality_autonomy.json"
DEFAULT_DAILY_SYNC_REPORT = ROOT / "reports/baselane_daily_sync_report.json"
DEFAULT_MONTHLY_RUN_REPORT = ROOT / "reports/baselane_financials_monthly_run_report.json"
DEFAULT_UNTAGGED_REVIEW_REPORT = ROOT / "reports/baselane_cf_untagged_review_packet.json"
DEFAULT_COOWNERSHIP_VALIDATION_REPORT = ROOT / "reports/coownership_gl_policy_validation.json"
DEFAULT_REPORTING_MONTH = datetime.now(timezone.utc).strftime("%Y-%m")
DEFAULT_MONTHLY_ACCRUALS_REPORT = ROOT / "reports" / f"baselane_monthly_accruals_{DEFAULT_REPORTING_MONTH.replace('-', '')}.json"
DEFAULT_MONTHLY_ACCRUALS_LIVE_PLAN = ROOT / "reports" / f"baselane_monthly_accruals_{DEFAULT_REPORTING_MONTH.replace('-', '')}.live-plan.json"
DEFAULT_MONTHLY_ACCRUALS_APPEND_AUDIT = ROOT / "reports/baselane_monthly_accrual_accidental_apply_audit.json"
DEFAULT_MONTHLY_ACCRUALS_APPEND_AUDIT_DECISION = ROOT / "config/baselane_monthly_accrual_append_audit_decision.json"
DEFAULT_LOFTY_MANAGER_PROPERTIES_RESPONSE = ROOT / "reports/lofty-pm-current/get-manager-properties.full-response.json"
DEFAULT_MISSING_RESERVE_DECISION_SCAFFOLD = (
    ROOT / "reports/lofty_financial_patch_readiness.missing-reserve-decisions.scaffold.json"
)
DEFAULT_PROPERTY_CASH_REVIEW_REPORTS = [ROOT / "reports/baselane_804_quitman_deficit_audit.json"]
DEFAULT_YHOME_CSV = ROOT / "reports/yhome_transition_reconciliation.csv"
DEFAULT_YHOME_UPDATE_PLAN = ROOT / "reports/yhome_operating_cash_update_plan.csv"
DEFAULT_REPORT = ROOT / "reports/baselane_lofty_transfer_requirements.json"
DEFAULT_MONTHLY_RECONCILIATION_REPORT = ROOT / "reports/baselane_monthly_transfer_reconciliation_report.json"
DEFAULT_LEGACY_MONTHLY_RECONCILIATION_REPORT = ROOT / "reports/baselane_monthly_transfer_reconciliation.json"
DEFAULT_CSV = ROOT / "reports/baselane_lofty_transfer_requirements.csv"
DEFAULT_CASH_BALANCE_CSV = ROOT / "reports/baselane_active_dao_cash_balances.csv"
DEFAULT_MD = ROOT / "reports/baselane_lofty_transfer_requirements.md"
DEFAULT_TELEGRAM_MD = ROOT / "reports/baselane_lofty_transfer_requirements.telegram.md"
DEFAULT_COWNERSHIP_STATES = ("NY", "CA", "HI", "FL", "CO")
DEFAULT_ECO_MINIMUM = 3000.0
INACTIVE_STATUS_MARKERS = ("sold", "selling", "closed", "delisted")
CONFLICT_THRESHOLD = 0.01
ECO_OPERATING_CASH_SOURCE_POLICY = (
    "ECO Operating Cash is the dated verified ECO-held unrestricted cash after "
    "recorded obligations and restrictions; this is spendable ECO Net DAO Funds."
)
ECO_OPERATING_CASH_REPORTING_MONTH_POLICY = (
    "The reporting month identifies the monthly close period for accrual/readiness gates only. "
    "ECO Operating Cash uses the dated cash-authority reconciliation through the close cutoff. "
    "The full property ledger and mapped physical bank cash remain separate controls."
)


def is_canonical_property_split_source(path: Path) -> bool:
    """Return whether a ledger is in the Dropbox property-split source folder."""
    parts = [part.casefold() for part in path.parts]
    filename = path.name.casefold()
    return (
        path.is_file()
        and filename.startswith("eco systems general ledger")
        and "real estate" in parts
        and any(part == "public" or part.endswith(" public") for part in parts)
        and "07 - p&l & owner statements" in parts
    )


def canonical_property_split_sources(source_path: Path) -> list[Path]:
    if not source_path.is_file() or not is_canonical_property_split_source(source_path):
        return [source_path]
    prefix = "eco systems general ledger - "
    candidates = [
        path
        for path in source_path.parent.glob("ECO Systems General Ledger*.csv")
        if is_canonical_property_split_source(path)
        and not any(marker in path.name.casefold() for marker in (".bak", "backup", "conflict"))
    ]
    if len(candidates) <= 1:
        return [source_path]

    def component_key(path: Path) -> str:
        stem = path.stem.casefold()
        tail = stem[len(prefix) :] if stem.startswith(prefix) else stem
        return normalize_property_name(tail.split(" - ", 1)[0].strip(" ."))

    def row_count(path: Path) -> int:
        try:
            with path.open(encoding="utf-8-sig", newline="") as handle:
                return max(sum(1 for _ in handle) - 1, 0)
        except OSError:
            return 0

    grouped: dict[str, list[Path]] = {}
    for path in candidates:
        grouped.setdefault(component_key(path), []).append(path)

    source_component = component_key(source_path)
    is_package = any("package" in normalize_property_name(part) for part in source_path.parts)
    selected: list[Path] = []
    for key, paths in grouped.items():
        if not is_package:
            # Non-package ledgers are one property.  Punctuation variants such
            # as ``St`` and ``St.`` are duplicate exports, not separate cash.
            # Divergent copies are never resolved by recency or row count.
            selected.append(resolve_equivalent_ledgers(paths))
            continue
        component_text = key
        exact = [
            path
            for path in paths
            if normalize_property_name(path.stem[len(prefix) :].split(" - ", 1)[0].strip(" .")) == component_text
            and " - " not in path.stem[len(prefix) :].strip(" .")
        ]
        pool = exact or paths
        selected.append(max(pool, key=lambda path: (row_count(path), path.stat().st_mtime, path.name.casefold())))

    if not is_package:
        # Keep the source path's component only; this prevents an unrelated
        # sibling export from being pulled into a single-property balance.
        selected = [path for path in selected if component_key(path) == source_component]
    return sorted(selected, key=lambda path: path.name.casefold())


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso_z(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "missing", "path": str(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"status": "unreadable", "path": str(path), "error": f"{type(exc).__name__}: {exc}"}
    return payload if isinstance(payload, dict) else {"status": "unreadable", "path": str(path), "error": "root is not object"}


def _find_lofty_property_rows(value: Any, depth: int = 0) -> list[dict[str, Any]] | None:
    """Find the current Lofty manager property list without trusting local documents."""
    if depth > 8:
        return None
    if isinstance(value, dict):
        for key in ("properties", "items", "rows"):
            candidate = value.get(key)
            if isinstance(candidate, list) and candidate and all(isinstance(item, dict) for item in candidate):
                if any(
                    any(str(field).casefold() in {"address", "address_line1", "propertyname", "property_name"} for field in item)
                    for item in candidate
                ):
                    return [dict(item) for item in candidate]
        for child in value.values():
            found = _find_lofty_property_rows(child, depth + 1)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_lofty_property_rows(child, depth + 1)
            if found is not None:
                return found
    return None


def lofty_reserve_authority(
    path: Path,
    records: list[dict[str, Any]],
    inactive_rows: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], list[str], dict[str, float | None]]:
    """Return current live Lofty reserves and fail closed on missing authority."""
    payload = read_json(path)
    payload_status = str(payload.get("status") or "").strip().lower()
    response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
    response_status = str(response.get("status") or "").strip().lower()
    status = payload_status or response_status
    property_rows = _find_lofty_property_rows(payload)
    active_candidates = []
    local_financials_only_candidates = []
    local_financials_only = set(LOCAL_FINANCIALS_ONLY_PROPERTIES)
    for record in records:
        property_name = str(record.get("property_name") or record.get("input_property_name") or "").strip()
        property_path = str(record.get("property_path") or record.get("input_property_path") or "")
        if property_name and state_from_property_path(property_path) and not inactive_yhome_match(property_name, inactive_rows):
            if canonical_reserve_property(property_name) in local_financials_only:
                local_financials_only_candidates.append(property_name)
            else:
                active_candidates.append(property_name)
    authority: dict[str, Any] = {
        "path": str(path),
        "status": "review",
        "source_status": status or "missing",
        "live_property_count": len(property_rows or []),
        "candidate_property_count": len(active_candidates),
        "local_financials_only_property_names": sorted(local_financials_only_candidates),
        "missing_property_names": [],
        "duplicate_property_names": [],
        "invalid_reserve_property_names": [],
        "source_mode": "lofty_manager_properties_live",
    }
    blockers: list[str] = []
    overrides: dict[str, float | None] = {}
    if payload_status in {"missing", "unreadable"} or response_status in {"missing", "unreadable"} or not path.is_file():
        blockers.append(f"lofty_reserve_authority_report_unavailable={path}")
        return authority, blockers, overrides
    if status != "ok":
        blockers.append(f"lofty_reserve_authority_status={status or 'missing'}")
        return authority, blockers, overrides
    if property_rows is None:
        blockers.append("lofty_reserve_authority_property_list_missing")
        return authority, blockers, overrides
    for property_name in active_candidates:
        matches = [
            item
            for item in property_rows
            if names_match(
                property_name,
                item.get("address")
                or item.get("address_line1")
                or item.get("propertyName")
                or item.get("property_name"),
            )
        ]
        key = normalize_property_name(property_name)
        if not matches:
            authority["missing_property_names"].append(property_name)
            blockers.append(f"lofty_reserve_authority_missing={property_name}")
            overrides[key] = None
            continue
        if len(matches) > 1:
            authority["duplicate_property_names"].append(property_name)
            blockers.append(f"lofty_reserve_authority_duplicate={property_name}")
            overrides[key] = None
            continue
        reserve = parse_money(matches[0].get("curr_maintenance_reserve"))
        if reserve is None:
            authority["invalid_reserve_property_names"].append(property_name)
            blockers.append(f"lofty_reserve_authority_invalid_reserve={property_name}")
            overrides[key] = None
            continue
        overrides[key] = reserve
    authority["status"] = "ok" if not blockers else "review"
    return authority, blockers, overrides


def write_json_report_with_alias(
    report: dict[str, Any], report_path: Path, *alias_paths: Path | None
) -> None:
    """Write the canonical report and every compatibility alias from one payload."""
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    destinations: list[Path] = []
    seen: set[Path] = set()
    for path in (report_path, *alias_paths):
        if path is None:
            continue
        resolved = path.expanduser().resolve(strict=False)
        if resolved not in seen:
            seen.add(resolved)
            destinations.append(path)

    temporary_paths: list[tuple[Path, Path]] = []
    try:
        for path in destinations:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
            temporary_path = Path(temporary_name)
            temporary_paths.append((path, temporary_path))
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
        for path, temporary_path in temporary_paths:
            os.replace(temporary_path, path)
    finally:
        for _, temporary_path in temporary_paths:
            if temporary_path.exists():
                temporary_path.unlink()


def source_blocker_summary(blockers: list[Any]) -> dict[str, dict[str, Any]]:
    categories: dict[str, dict[str, Any]] = {
        "monthly_accruals": {
            "count": 0,
            "sample": [],
            "next_action": "Resolve accrual append audit and gap approvals before transfer totals can be final.",
        },
        "baselane_auth": {
            "count": 0,
            "sample": [],
            "next_action": (
                "Solve Baselane reCAPTCHA/login in the visible CDP tab, then rerun monthly finance-truth refresh "
                "before final transfer totals."
            ),
        },
        "missing_lofty_reserve": {
            "count": 0,
            "sample": [],
            "next_action": "Refresh and verify the current live Lofty manager property response before using reserve values.",
        },
        "cf_balance_sheet": {
            "count": 0,
            "sample": [],
            "next_action": (
                "Clear authoritative CF balance-sheet consistency issues. "
                "Yhome spreadsheet work-product issues are reported separately and do not block transfers."
            ),
        },
        "property_cash_review": {
            "count": 0,
            "sample": [],
            "next_action": "Complete property cash review decisions before moving money.",
        },
        "required_source": {
            "count": 0,
            "sample": [],
            "next_action": "Regenerate or restore required source artifacts before transfer reconciliation.",
        },
        "other": {
            "count": 0,
            "sample": [],
            "next_action": "Review remaining transfer source blockers.",
        },
    }

    def category_for(blocker: str) -> str:
        lowered = blocker.lower()
        if (
            lowered.startswith("monthly_accruals_live_plan_")
            and ("auth_blocked" in lowered or "cdp_blocked" in lowered or "recaptcha" in lowered)
        ):
            return "baselane_auth"
        if "monthly_accruals_" in lowered:
            return "monthly_accruals"
        if lowered.startswith("missing_lofty_reserve_decision_") or lowered.startswith("lofty_reserve_authority_"):
            return "missing_lofty_reserve"
        if lowered.startswith("cf_balance_sheet_"):
            return "cf_balance_sheet"
        if lowered.startswith("property_cash_review:"):
            return "property_cash_review"
        if lowered.startswith("required_source_"):
            return "required_source"
        return "other"

    for raw_blocker in blockers:
        blocker = str(raw_blocker or "").strip()
        if not blocker:
            continue
        category = categories[category_for(blocker)]
        category["count"] += 1
        if len(category["sample"]) < 5:
            category["sample"].append(blocker)

    return {name: details for name, details in categories.items() if details["count"]}


def missing_reserve_decision_blockers(path: Path) -> tuple[list[str], dict[str, Any]]:
    payload = read_json(path)
    payload["path"] = str(path)
    if payload.get("status") in {"missing", "unreadable"}:
        return [], payload
    records = payload.get("records") if isinstance(payload.get("records"), list) else []
    blockers: list[str] = []
    unreviewed = 0
    missing_amount = 0
    missing_source = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("reviewed") is not True:
            unreviewed += 1
        if parse_money(record.get("curr_maintenance_reserve")) is None:
            missing_amount += 1
        if not str(record.get("curr_maintenance_reserve_source") or "").strip():
            missing_source += 1
    if unreviewed:
        blockers.append(f"missing_lofty_reserve_decision_unreviewed_count={unreviewed}")
    if missing_amount:
        blockers.append(f"missing_lofty_reserve_decision_amount_missing_count={missing_amount}")
    if missing_source:
        blockers.append(f"missing_lofty_reserve_decision_source_missing_count={missing_source}")
    return blockers, {
        "path": str(path),
        "status": "ok" if not blockers else "review",
        "record_count": len(records),
        "unreviewed_count": unreviewed,
        "amount_missing_count": missing_amount,
        "source_missing_count": missing_source,
        "blockers": blockers,
    }


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_ledger_evidence(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Capture the exact property-split ECO ledgers used for the cash total."""
    paths: set[Path] = set()
    for row in rows:
        source_text = str(row.get("eco_gl_column_e_source") or "").strip()
        for source in source_text.split(";"):
            source = source.strip()
            if source:
                paths.add(Path(source).expanduser().resolve(strict=False))

    sources = [
        {
            "path": str(path),
            "sha256": sha256_file(path),
        }
        for path in sorted(paths, key=lambda value: str(value).casefold())
    ]
    missing_paths = [source["path"] for source in sources if source["sha256"] is None]
    fingerprint_text = "\n".join(
        f"{source['path']}\0{source['sha256']}" for source in sources if source["sha256"]
    )
    return {
        "status": "ok" if sources and not missing_paths else "review",
        "source_count": len(sources),
        "missing_source_count": len(missing_paths),
        "missing_source_paths": missing_paths,
        "sources": sources,
        "fingerprint_sha256": hashlib.sha256(fingerprint_text.encode("utf-8")).hexdigest() if sources and not missing_paths else None,
        "policy": "Fingerprint covers every resolved property-split ECO GL source used for active DAO cash balances.",
    }


def monthly_accrual_report_for_month(month: str | None) -> Path:
    text = str(month or "").strip()
    match = re.fullmatch(r"(\d{4})-(\d{2})", text)
    if not match:
        return DEFAULT_MONTHLY_ACCRUALS_REPORT
    return ROOT / "reports" / f"baselane_monthly_accruals_{match.group(1)}{match.group(2)}.json"


def parse_money(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    text = str(value).strip()
    if not text or text in {"-", "—"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.replace("$", "").replace(",", "").replace("(", "").replace(")", "").strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return round(-number if negative else number, 2)


def parse_amount_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in {"-", "—"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    normalized = text.replace("$", "").replace(",", "").replace("(", "").replace(")", "").strip()
    if not normalized or normalized in {"-", "—"}:
        return None
    try:
        amount = Decimal(normalized)
    except InvalidOperation:
        return None
    return -amount if negative else amount


def sum_money_values(values: Iterable[Any]) -> float:
    """Add monetary values in decimal cents before serializing to JSON."""
    total = sum((Decimal(str(value)) for value in values if value is not None), Decimal("0"))
    return float(total.quantize(Decimal("0.01")))


def invalidate_eco_source(source: dict[str, Any], status: str, reason: str) -> dict[str, Any]:
    source["eco_gl_column_e_sum"] = None
    source["eco_general_ledger_sum"] = None
    source["eco_gl_column_e_status"] = status
    source["eco_gl_column_e_scope"] = None
    source["eco_gl_column_e_source_mode"] = "canonical_source_invalid"
    source["eco_gl_column_e_source_error"] = reason
    return source


def money(value: float | int | None) -> str:
    if value is None:
        return ""
    number = round(float(value), 2)
    if number < 0:
        return f"$({abs(number):,.2f})"
    return f"${number:,.2f}"


def compact_money(value: float | int | None) -> str:
    if value is None:
        return "held"
    number = round(float(value), 2)
    sign = "-" if number < 0 else ""
    return f"{sign}${abs(number):,.2f}"


def normalize_property_name(value: Any) -> str:
    text = str(value or "").strip().lower().replace("&", " and ")
    text = re.sub(r",?\s+(al|ar|ca|co|fl|ga|hi|ia|il|mi|mo|ny|oh|sc|tn|tx|ut|wa)\s+\d{5}(?:-\d{4})?", " ", text)
    text = re.sub(r",?\s+(al|ar|ca|co|fl|ga|hi|ia|il|mi|mo|ny|oh|sc|tn|tx|ut|wa)\s*$", " ", text)
    text = re.sub(r"\b(public|dao|llc|lfty\d+)\b", " ", text)
    replacements = {
        "avenue": "ave",
        "street": "st",
        "road": "rd",
        "lane": "ln",
        "drive": "dr",
        "place": "pl",
        "circle": "cir",
        "north": "n",
        "south": "s",
        "east": "e",
        "west": "w",
    }
    for source, target in replacements.items():
        text = re.sub(rf"\b{source}\b", target, text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def names_match(left: Any, right: Any) -> bool:
    left_key = normalize_property_name(left)
    right_key = normalize_property_name(right)
    return bool(left_key and right_key and (left_key == right_key or left_key in right_key or right_key in left_key))


def state_from_property_path(path_value: Any) -> str | None:
    parts = Path(str(path_value or "")).parts
    for index, part in enumerate(parts):
        if part == "Real Estate" and index + 1 < len(parts):
            state = parts[index + 1].upper()
            return state if re.fullmatch(r"[A-Z]{2}", state) else None
    return None


def candidate_records(path: Path) -> list[dict[str, Any]]:
    payload = read_json(path)
    records = payload.get("records")
    return [record for record in records if isinstance(record, dict)] if isinstance(records, list) else []


def candidate_packet_blockers(payload: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    status = str(payload.get("status") or "")
    if status in {"missing", "unreadable"}:
        blockers.append(f"candidate_packet_status={status}")
    manifest_record_count = int(payload.get("manifest_record_count") or 0)
    packet_records = payload.get("records")
    candidate_record_count = len(packet_records) if isinstance(packet_records, list) else 0
    skipped_excluded_record_count = int(payload.get("skipped_excluded_record_count") or 0)
    expected_record_count = max(0, manifest_record_count - skipped_excluded_record_count) if manifest_record_count else 0
    if expected_record_count and candidate_record_count < expected_record_count:
        blockers.append(f"candidate_packet_records_partial={candidate_record_count}/{expected_record_count}")
    empty_reason = str(payload.get("empty_candidate_packet_reason") or "").strip()
    if empty_reason:
        blockers.append(f"candidate_packet_empty_reason={empty_reason}")
    review_manifest_source_issues = payload.get("review_manifest_source_issues")
    if isinstance(review_manifest_source_issues, list):
        blockers.extend(f"review_manifest_source_issue={issue}" for issue in review_manifest_source_issues if issue)
    return blockers


def required_source_artifact_blockers(
    *,
    candidate_packet_payload: dict[str, Any],
    candidate_packet_path: Path,
    source_cleanup_queue: dict[str, Any],
    source_cleanup_queue_path: Path,
    source_cash_report: dict[str, Any],
    source_cash_report_path: Path,
    source_cash_reconciliation_actions: dict[str, Any],
    source_cash_reconciliation_actions_path: Path,
    ecogl_autonomy_report: dict[str, Any],
    ecogl_autonomy_report_path: Path,
    monthly_accruals_report: dict[str, Any],
    monthly_accruals_report_path: Path,
    cf_balance_sheet_report: dict[str, Any],
    cf_balance_sheet_report_path: Path,
    current_run_started_at: str | None = None,
) -> list[str]:
    blockers: list[str] = []
    required_sources = [
        ("candidate_packet", candidate_packet_payload, candidate_packet_path),
        ("source_cleanup_queue", source_cleanup_queue, source_cleanup_queue_path),
        ("source_cash_report", source_cash_report, source_cash_report_path),
        ("source_cash_reconciliation_actions", source_cash_reconciliation_actions, source_cash_reconciliation_actions_path),
        ("ecogl_autonomy_report", ecogl_autonomy_report, ecogl_autonomy_report_path),
        ("monthly_accruals_report", monthly_accruals_report, monthly_accruals_report_path),
        ("cf_balance_sheet_report", cf_balance_sheet_report, cf_balance_sheet_report_path),
    ]
    for label, payload, path in required_sources:
        status = str(payload.get("status") or "").strip()
        if status in {"missing", "unreadable"}:
            blockers.append(f"required_source_{label}_status={status}")
        if not path.is_file():
            blockers.append(f"required_source_{label}_file_missing")
    current_run_started = parse_iso_z(current_run_started_at)
    if current_run_started:
        # These artifacts are produced by the current monthly run.  Never
        # reuse an older CF audit after an upstream step failed before it was
        # regenerated; that can otherwise make a disabled/stale audit look
        # like current evidence.
        run_scoped_sources = {
            "candidate_packet": candidate_packet_payload,
            "monthly_accruals_report": monthly_accruals_report,
            "cf_balance_sheet_report": cf_balance_sheet_report,
        }
        for label, payload in run_scoped_sources.items():
            generated_at = parse_iso_z(payload.get("generated_at"))
            if generated_at is None:
                blockers.append(f"required_source_{label}_current_monthly_run_timestamp_missing")
            elif generated_at < current_run_started:
                blockers.append(f"required_source_{label}_older_than_current_monthly_run")
    return blockers


def ecogl_autonomy_blockers(report: dict[str, Any]) -> list[str]:
    """Fail closed when the native Baselane source-quality gate holds downstream work."""
    if report.get("downstream_hold") is not True:
        return []
    count = int(report.get("exception_count") or 0)
    return [f"ecogl_autonomy_downstream_hold_exceptions={count}"]


NON_SOURCE_MONTHLY_FAILURE_STEP_PREFIXES = (
    "discord_",
    "guild_",
    "lofty_",
    "monthly_owner_review_gate",
    "monthly_readiness",
    "non_native_owner_email",
    "owner_email",
    "pipeline_candidate_coverage",
    "transfer_reconciliation",
    "yhome_",
)


def monthly_run_failure_blocks_transfer(report: dict[str, Any]) -> bool:
    status = str(report.get("effective_status") or report.get("status") or "").strip().lower()
    if status == "ok":
        return False
    failed_step = str(report.get("effective_failed_step") or report.get("failed_step") or "").strip()
    if not failed_step:
        return True
    return not failed_step.startswith(NON_SOURCE_MONTHLY_FAILURE_STEP_PREFIXES)


def operational_runtime_blockers(
    *,
    candidate_packet_payload: dict[str, Any],
    daily_sync_report: dict[str, Any],
    daily_sync_report_path: Path,
    monthly_run_report: dict[str, Any],
    monthly_run_report_path: Path,
    current_run_started_at: str | None = None,
) -> list[str]:
    """Prevent final transfer certification from using a failed or stale run."""
    blockers: list[str] = []
    # The monthly report is written by the EXIT trap, after this transfer
    # step.  During an active cron run its path necessarily points to the
    # previous run, so validating it here would either trust stale evidence or
    # block every successful run.  The current run start is the authoritative
    # context until the final report is written.
    required_reports = [("daily_sync", daily_sync_report, daily_sync_report_path)]
    if not current_run_started_at:
        required_reports.append(("monthly_run", monthly_run_report, monthly_run_report_path))
    for label, payload, path in required_reports:
        if not path.is_file():
            blockers.append(f"operational_{label}_report_file_missing")
            continue
        status = str(payload.get("effective_status") or payload.get("status") or "").strip().lower()
        if status != "ok" and (
            label != "monthly_run" or monthly_run_failure_blocks_transfer(payload)
        ):
            blockers.append(f"operational_{label}_status={status or 'missing'}")
        if label == "daily_sync":
            daily_run_status = str(payload.get("daily_run_status") or "").strip().lower()
            # A standalone sync can recover a wrapper failure (for example an
            # auth-preflight failure) and writes that result to
            # ``effective_status``.  Keep the original wrapper result in the
            # report, but do not turn a proven recovered sync into a transfer
            # blocker.
            if daily_run_status and daily_run_status != "ok" and status != "ok":
                blockers.append(f"operational_daily_run_status={daily_run_status}")
        generated_at = parse_iso_z(
            payload.get("generated_at")
            or payload.get("ended_at")
            or payload.get("completed_at")
        )
        if generated_at is None:
            blockers.append(f"operational_{label}_report_timestamp_missing")
    return blockers


def financial_summary(record: dict[str, Any]) -> dict[str, Any]:
    summary = record.get("monthly_financial_summary")
    source = dict(summary) if isinstance(summary, dict) else {}
    ledger_path = Path(str(source.get("eco_gl_column_e_source") or ""))
    try:
        source_paths = canonical_property_split_sources(ledger_path)
    except DivergentCanonicalLedgerError as exc:
        return invalidate_eco_source(
            source,
            "ambiguous_canonical_source",
            f"multiple divergent property ledgers: {', '.join(path.name for path in exc.paths)}",
        )
    if not source_paths or not all(path.is_file() for path in source_paths):
        return source
    if not all(is_canonical_property_split_source(path) for path in source_paths):
        source["eco_gl_column_e_sum"] = None
        source["eco_operating_cash"] = None
        source["eco_gl_column_e_status"] = "missing_canonical"
        source["eco_gl_column_e_scope"] = None
        source["eco_gl_column_e_source_mode"] = "noncanonical_source"
        return source
    try:
        total = Decimal("0")
        settlement_basis_total = Decimal("0")
        row_count = 0
        excluded_count = 0
        excluded_amount = Decimal("0")
        for source_path in source_paths:
            with source_path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            amount_header = next(
                (header for header in (rows[0].keys() if rows else []) if str(header).strip().casefold() == "amount"),
                None,
            )
            if not rows:
                return invalidate_eco_source(source, "empty_canonical_source", f"no ledger rows: {source_path.name}")
            if not amount_header:
                return invalidate_eco_source(source, "invalid_canonical_source", f"Amount column missing: {source_path.name}")
            for row in rows:
                amount_text = str(row.get(amount_header) or "").strip()
                if not amount_text:
                    continue
                amount = parse_amount_decimal(amount_text)
                if amount is None:
                    return invalidate_eco_source(source, "invalid_canonical_source", f"invalid Amount value: {amount_text}")
                total += amount
                row_count += 1
                if is_cash_basis_excluded_row(row):
                    excluded_count += 1
                    excluded_amount += amount
                else:
                    settlement_basis_total += amount
    except (OSError, InvalidOperation, csv.Error) as exc:
        return invalidate_eco_source(source, "unreadable_canonical_source", f"{type(exc).__name__}: {exc}")
    if row_count == 0:
        return invalidate_eco_source(source, "empty_canonical_source", "no non-empty Amount values")
    source["eco_gl_column_e_sum"] = float(total.quantize(Decimal("0.01")))
    source["eco_gl_column_e_row_count"] = row_count
    source["eco_gl_column_e_status"] = "ok"
    source["eco_gl_column_e_scope"] = "all_property_split_rows"
    source["eco_gl_column_e_source"] = "; ".join(str(path) for path in source_paths)
    source["eco_gl_column_e_sources"] = [str(path) for path in source_paths]
    source["eco_gl_column_e_source_mode"] = (
        "canonical_property_split_gl_package_aggregate" if len(source_paths) > 1 else "canonical_property_split_gl"
    )
    source["cash_settlement_basis_sum"] = float(settlement_basis_total.quantize(Decimal("0.01")))
    source["cash_settlement_basis_scope"] = "all_property_split_rows_excluding_non_cash_closes_and_accrual_journals"
    source["eco_general_ledger_sum"] = source["eco_gl_column_e_sum"]
    source["non_cash_close_row_count_excluded_from_settlement"] = excluded_count
    source["non_cash_close_amount_excluded_from_settlement"] = float(excluded_amount.quantize(Decimal("0.01")))
    source["eco_gl_column_e_runtime_refreshed"] = True
    return source


def active_dao_cash_balance_rows(
    records: list[dict[str, Any]],
    inactive_rows: dict[str, dict[str, Any]],
    lofty_reserve_overrides: dict[str, float | None] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in records:
        property_name = str(record.get("property_name") or record.get("input_property_name") or "").strip()
        property_path = str(record.get("property_path") or record.get("input_property_path") or "")
        state = state_from_property_path(property_path)
        if not property_name or not state or inactive_yhome_match(property_name, inactive_rows):
            continue
        summary = financial_summary(record)
        eco_gl = parse_money(summary.get("eco_gl_column_e_sum"))
        eco_cash = parse_money(summary.get("eco_operating_cash"))
        physical_bank_cash = parse_money(summary.get("physical_bank_cash"))
        reserve_key = normalize_property_name(property_name)
        reserve = (
            lofty_reserve_overrides[reserve_key]
            if lofty_reserve_overrides is not None and reserve_key in lofty_reserve_overrides
            else parse_money(summary.get("lofty_curr_maintenance_reserve"))
        )
        rows.append(
            {
                "property": property_name,
                "state": state,
                "property_path": property_path,
                "eco_operating_cash": eco_cash,
                "eco_gl_column_e_sum": eco_gl,
                "physical_bank_cash": physical_bank_cash,
                "physical_bank_cash_status": summary.get("physical_bank_cash_status"),
                "physical_bank_cash_source_mode": summary.get("physical_bank_cash_source_mode"),
                "physical_bank_cash_source": summary.get("physical_bank_cash_source"),
                "physical_bank_cash_as_of_date": summary.get("physical_bank_cash_as_of_date"),
                "bank_minus_gl_gap": (
                    round(physical_bank_cash - eco_gl, 2)
                    if physical_bank_cash is not None and eco_gl is not None
                    else None
                ),
                "eco_gl_column_e_scope": summary.get("eco_gl_column_e_scope"),
                "eco_gl_column_e_status": summary.get("eco_gl_column_e_status"),
                "eco_gl_column_e_row_count": summary.get("eco_gl_column_e_row_count"),
                "eco_gl_column_e_source": summary.get("eco_gl_column_e_source"),
                "eco_gl_column_e_source_mode": summary.get("eco_gl_column_e_source_mode"),
                "lofty_curr_maintenance_reserve": reserve,
                "combined_eco_and_lofty_reserve": sum_money_values((eco_cash, reserve))
                if eco_cash is not None
                else None,
                "eco_operating_cash_balance_basis": summary.get("eco_operating_cash_source_mode"),
                "eco_operating_cash_balance_scope": summary.get("eco_operating_cash_balance_scope"),
                "eco_operating_cash_source": summary.get("eco_operating_cash_source"),
                "eco_operating_cash_as_of_date": summary.get("eco_operating_cash_as_of_date"),
                "eco_documented_security_principal": parse_money(
                    summary.get("eco_documented_security_principal")
                ),
                "eco_operating_float": parse_money(summary.get("eco_operating_float")),
                "eco_protected_minimum": parse_money(summary.get("eco_protected_minimum")),
                "eco_bank_account_count": summary.get("eco_bank_account_count"),
                "cash_balance_status": (
                    "ok"
                    if eco_cash is not None
                    and summary.get("eco_operating_cash_status") == "ok"
                    and summary.get("eco_gl_column_e_status") == "ok"
                    and summary.get("eco_gl_column_e_scope") in {None, "", "all_property_split_rows"}
                    else "missing_source"
                ),
                "eco_gl_column_e_reporting_month": summary.get("as_of_month"),
            }
        )
    return sorted(rows, key=lambda row: (row["state"], row["property"].lower()))


def active_dao_cash_balance_integrity_blockers(
    rows: list[dict[str, Any]],
    *,
    tolerance: float = 0.01,
) -> list[str]:
    """Reject duplicate or internally inconsistent active DAO cash rows."""
    blockers: list[str] = []
    properties: dict[str, list[str]] = {}
    source_owners: dict[str, list[str]] = {}
    for row in rows:
        property_name = str(row.get("property") or "").strip()
        property_key = normalize_property_name(property_name)
        if property_key:
            properties.setdefault(property_key, []).append(property_name)

        source_status = str(row.get("eco_gl_column_e_status") or "").strip()
        source_scope = str(row.get("eco_gl_column_e_scope") or "").strip()
        if source_status and source_status != "ok":
            blockers.append(
                f"source_cash_active_dao_source_status={property_name}:{source_status or 'missing'}"
            )
        if source_scope and source_scope != "all_property_split_rows":
            blockers.append(
                f"source_cash_active_dao_source_scope={property_name}:{source_scope or 'missing'}"
            )

        eco_value = parse_money(row.get("eco_operating_cash"))
        if eco_value is None or row.get("cash_balance_status") != "ok":
            blockers.append(
                f"source_cash_active_dao_gl_authority_invalid={property_name}"
            )

        source_text = str(row.get("eco_gl_column_e_source") or "").strip()
        for source in (part.strip() for part in source_text.split(";")):
            if not source:
                continue
            source_key = str(Path(source).resolve(strict=False)).casefold()
            source_owners.setdefault(source_key, []).append(property_name)

    for property_key, names in sorted(properties.items()):
        if len(names) > 1:
            blockers.append(
                f"source_cash_duplicate_active_property={property_key}:{' | '.join(sorted(names))}"
            )
    for source_key, owners in sorted(source_owners.items()):
        unique_owners = sorted(set(owner for owner in owners if owner))
        if len(unique_owners) > 1:
            blockers.append(
                f"source_cash_duplicate_canonical_ledger={source_key}:{' | '.join(unique_owners)}"
            )
    return sorted(set(blockers))


def write_cash_balance_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "property",
        "state",
        "eco_operating_cash",
        "eco_gl_column_e_sum",
        "physical_bank_cash",
        "physical_bank_cash_status",
        "physical_bank_cash_source_mode",
        "physical_bank_cash_source",
        "physical_bank_cash_as_of_date",
        "lofty_curr_maintenance_reserve",
        "combined_eco_and_lofty_reserve",
        "cash_balance_status",
        "eco_gl_column_e_reporting_month",
        "eco_gl_column_e_row_count",
        "eco_gl_column_e_scope",
        "eco_gl_column_e_source",
        "eco_gl_column_e_source_mode",
        "eco_operating_cash_balance_basis",
        "eco_operating_cash_as_of_date",
        "bank_minus_gl_gap",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows({field: row.get(field) for field in fieldnames} for row in rows)


def cf_summary_index(path: Path) -> dict[str, dict[str, Any]]:
    payload = read_json(path)
    summaries = payload.get("summaries")
    if not isinstance(summaries, list):
        return {}
    indexed: dict[str, dict[str, Any]] = {}
    for summary in summaries:
        if not isinstance(summary, dict):
            continue
        key = normalize_property_name(summary.get("property"))
        if key:
            indexed[key] = summary
    return indexed


def cf_balance_cross_artifact_mismatches(
    active_cash_rows: list[dict[str, Any]],
    cf_index: dict[str, dict[str, Any]],
    *,
    tolerance: float = 0.01,
) -> list[dict[str, Any]]:
    """Require the audited CF month to agree with its canonical source value.

    The CF cell for a closed month is a month-end balance, while the active
    cash ledger is the full current Column E balance.  Those are intentionally
    different reporting measures and must not be compared as if they were the
    same number.
    """
    mismatches: list[dict[str, Any]] = []
    for cash_row in active_cash_rows:
        property_name = str(cash_row.get("property") or "").strip()
        key = normalize_property_name(property_name)
        summary = cf_index.get(key)
        if not summary:
            mismatches.append(
                {
                    "property": property_name,
                    "type": "cf_summary_missing_for_active_dao",
                    "eco_operating_cash": cash_row.get("eco_operating_cash"),
                }
            )
            continue

        source_value = parse_money(cash_row.get("eco_operating_cash"))
        gl_source_value = parse_money(cash_row.get("eco_gl_column_e_sum"))
        if (
            summary.get("eco_balance_semantics")
            == "full_canonical_property_general_ledger_net_position_including_accruals"
        ):
            expected = parse_money(summary.get("eco_general_ledger_expected"))
            if expected is None:
                expected = parse_money(summary.get("eco_full_balance"))
            actual = parse_money(summary.get("eco_general_ledger_actual"))
            if gl_source_value is None or expected is None:
                mismatches.append(
                    {
                        "property": property_name,
                        "type": "cf_balance_value_missing",
                        "field": "eco_general_ledger_expected",
                        "eco_general_ledger": gl_source_value,
                        "cf_value": expected,
                    }
                )
            elif abs(gl_source_value - expected) > tolerance:
                mismatches.append(
                    {
                        "property": property_name,
                        "type": "cf_balance_mismatch",
                        "field": "eco_general_ledger_expected",
                        "eco_general_ledger": gl_source_value,
                        "cf_value": expected,
                        "difference": round(gl_source_value - expected, 2),
                    }
                )
            if summary.get("workbook_audit_status") == "skipped_live_workbook_io_disabled":
                continue
            if expected is None or actual is None:
                mismatches.append(
                    {
                        "property": property_name,
                        "type": "cf_balance_value_missing",
                        "field": "eco_general_ledger_actual",
                        "eco_general_ledger": gl_source_value,
                        "cf_value": actual,
                    }
                )
            elif abs(expected - actual) > tolerance:
                mismatches.append(
                    {
                        "property": property_name,
                        "type": "cf_balance_mismatch",
                        "field": "eco_general_ledger_actual",
                        "eco_general_ledger": gl_source_value,
                        "cf_value": actual,
                        "expected": expected,
                        "difference": round(expected - actual, 2),
                    }
                )
            cash_expected = parse_money(summary.get("eco_operating_cash_expected"))
            cash_actual = parse_money(summary.get("eco_operating_cash_actual"))
            if source_value is None or cash_expected is None:
                mismatches.append(
                    {
                        "property": property_name,
                        "type": "cf_balance_value_missing",
                        "field": "eco_operating_cash_expected",
                        "eco_operating_cash": source_value,
                        "cf_value": cash_expected,
                    }
                )
            elif abs(source_value - cash_expected) > tolerance:
                mismatches.append(
                    {
                        "property": property_name,
                        "type": "cf_balance_mismatch",
                        "field": "eco_operating_cash_expected",
                        "eco_operating_cash": source_value,
                        "cf_value": cash_expected,
                        "difference": round(source_value - cash_expected, 2),
                    }
                )
            if cash_expected is None or cash_actual is None:
                mismatches.append(
                    {
                        "property": property_name,
                        "type": "cf_balance_value_missing",
                        "field": "eco_operating_cash_actual",
                        "eco_operating_cash": source_value,
                        "cf_value": cash_actual,
                    }
                )
            elif abs(cash_expected - cash_actual) > tolerance:
                mismatches.append(
                    {
                        "property": property_name,
                        "type": "cf_balance_mismatch",
                        "field": "eco_operating_cash_actual",
                        "eco_operating_cash": source_value,
                        "cf_value": cash_actual,
                        "expected": cash_expected,
                        "difference": round(cash_expected - cash_actual, 2),
                    }
                )
            continue
        if summary.get("eco_balance_semantics"):
            expected = parse_money(summary.get("eco_operating_cash_expected"))
            actual = parse_money(summary.get("eco_operating_cash_actual"))
            if expected is None:
                mismatches.append(
                    {
                        "property": property_name,
                        "type": "cf_balance_value_missing",
                        "field": "historical_eco_operating_cash",
                        "eco_operating_cash": source_value,
                        "cf_value": actual,
                    }
                )
            elif summary.get("workbook_audit_status") == "skipped_live_workbook_io_disabled":
                # The expected value is sourced from the canonical closed-month
                # ledger. The separate workbook-audit gate records that no cell
                # was read, so this cross-check must not manufacture a mismatch.
                continue
            elif actual is None:
                mismatches.append(
                    {
                        "property": property_name,
                        "type": "cf_balance_value_missing",
                        "field": "historical_eco_operating_cash",
                        "eco_operating_cash": source_value,
                        "cf_value": actual,
                    }
                )
            elif abs(expected - actual) > tolerance:
                mismatches.append(
                    {
                        "property": property_name,
                        "type": "cf_balance_mismatch",
                        "field": "historical_eco_operating_cash",
                        "eco_operating_cash": source_value,
                        "cf_value": actual,
                        "expected": expected,
                        "difference": round(expected - actual, 2),
                    }
                )
            continue
        compared_values: list[tuple[str, float | None]] = []
        for field in ("eco_operating_cash_expected", "eco_operating_cash_actual", "eco_general_ledger_expected"):
            if field in summary:
                compared_values.append((field, parse_money(summary.get(field))))
        for field, cf_value in compared_values:
            if source_value is None or cf_value is None:
                mismatches.append(
                    {
                        "property": property_name,
                        "type": "cf_balance_value_missing",
                        "field": field,
                        "eco_operating_cash": source_value,
                        "cf_value": cf_value,
                    }
                )
            elif abs(source_value - cf_value) > tolerance:
                mismatches.append(
                    {
                        "property": property_name,
                        "type": "cf_balance_mismatch",
                        "field": field,
                        "eco_operating_cash": source_value,
                        "cf_value": cf_value,
                        "difference": round(source_value - cf_value, 2),
                    }
                )
    return mismatches


def load_yhome_inactive_rows(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        property_header = next((header for header in headers if header.strip().lower() == "property"), None)
        new_pm_header = next((header for header in headers if header.strip().lower() == "new pm"), None)
        if not property_header or not new_pm_header:
            return {}
        inactive: dict[str, dict[str, Any]] = {}
        for row_number, row in enumerate(reader, start=2):
            status = str(row.get(new_pm_header) or "").strip()
            if not any(marker in status.lower() for marker in INACTIVE_STATUS_MARKERS):
                continue
            key = normalize_property_name(row.get(property_header))
            if key:
                inactive[key] = {"row": row_number, "status": status}
        return inactive


def load_inactive_exclusion_rows(path: Path) -> dict[str, dict[str, Any]]:
    inactive = load_yhome_inactive_rows(path)
    guards, _yhome_guard, _manual_exclusions = monthly_exclusion_guards(path)
    for guard in guards:
        source = str(guard.get("source") or "monthly_exclusion").strip()
        if source == "operational_ignore_listing_updates":
            continue
        property_name = str(guard.get("property_name") or "").strip()
        key = normalize_property_name(property_name)
        if not key:
            continue
        payload = {
            "row": None,
            "status": str(guard.get("exclude_reason") or source or "excluded").strip(),
            "source": source,
        }
        existing = inactive.get(key)
        if not existing or (
            source == "sold_ignore_listing_updates"
            and str(existing.get("source") or "").strip() == "manual_exclusion"
        ):
            inactive[key] = payload
    return inactive


def inactive_yhome_match(property_name: str, inactive_rows: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    key = normalize_property_name(property_name)
    if key in inactive_rows:
        return inactive_rows[key]
    for inactive_key, payload in inactive_rows.items():
        if names_match(key, inactive_key):
            return payload
    return None


def load_yhome_update_required_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if str(row.get("action") or "").strip().lower() == "update":
                    rows.append(dict(row))
    except OSError:
        return []
    return rows


def yhome_update_required_details(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for row in rows:
        property_name = str(row.get("property") or "").strip()
        column = str(row.get("column") or "").strip()
        if not property_name or not column:
            continue
        details.append(
            {
                "property": property_name,
                "row_number": row.get("yhome_row_number"),
                "column": column,
                "current_value": parse_money(row.get("current_value")),
                "target_value": parse_money(row.get("target_value")),
                "target_value_formatted": row.get("target_value_formatted"),
                "diff": parse_money(row.get("diff")),
                "cell_hint": None,
                "action": row.get("action"),
            }
        )
    return details


def source_blockers(
    source_cleanup_queue: dict[str, Any],
    source_cash_report: dict[str, Any],
    source_cash_reconciliation_actions: dict[str, Any],
) -> list[str]:
    blockers: list[str] = []
    cleanup_actions = source_cleanup_queue.get("actions")
    cleanup_actions = cleanup_actions if isinstance(cleanup_actions, list) else []
    cleanup_generated_at = parse_iso_z(source_cleanup_queue.get("generated_at"))
    source_cash_generated_at = parse_iso_z(source_cash_report.get("generated_at"))
    raw_guard = source_cash_report.get("raw_no_dao_mortgage_guard")
    raw_guard = raw_guard if isinstance(raw_guard, dict) else {}
    deprecated_raw_mortgage_queue = bool(cleanup_actions) and all(
        str(action.get("action") or "") == "remove_no_dao_mortgage_source_row"
        for action in cleanup_actions
        if isinstance(action, dict)
    )
    cleanup_queue_superseded = (
        deprecated_raw_mortgage_queue
        and source_cash_report.get("status") == "ok"
        and raw_guard.get("active") is False
        and cleanup_generated_at is not None
        and source_cash_generated_at is not None
        and source_cash_generated_at > cleanup_generated_at
    )
    if int(source_cleanup_queue.get("action_count") or 0) > 0 and not cleanup_queue_superseded:
        blockers.append(f"source_cleanup_queue_actions={int(source_cleanup_queue.get('action_count') or 0)}")
    if int(source_cleanup_queue.get("missing_id_count") or 0) > 0 and not cleanup_queue_superseded:
        blockers.append(f"source_cleanup_queue_missing_ids={int(source_cleanup_queue.get('missing_id_count') or 0)}")
    if bool(source_cash_report.get("apply_blocked_by_raw_no_dao_mortgage_guard")):
        blockers.append("source_cash_apply_blocked_by_raw_no_dao_mortgage_guard")
    active_action_count = int(source_cash_reconciliation_actions.get("active_monthly_candidate_action_count") or 0)
    actions_status = source_cash_reconciliation_actions.get("status")
    actions_available = actions_status not in {"missing", "unreadable", None}
    actions_generated_at = parse_iso_z(source_cash_reconciliation_actions.get("generated_at"))
    actions_stale = bool(source_cash_generated_at and actions_generated_at and actions_generated_at < source_cash_generated_at)
    source_cash_digest = str(source_cash_report.get("source_cash_report_digest") or source_cash_report.get("report_digest") or "").strip()
    actions_source_cash_digest = str(source_cash_reconciliation_actions.get("source_cash_report_digest") or "").strip()
    digest_mismatch = bool(
        source_cash_digest
        and actions_source_cash_digest
        and source_cash_digest != actions_source_cash_digest
    )
    reconciliation_authoritative = actions_available and not actions_stale and not digest_mismatch
    raw_violation_count = int(source_cash_report.get("violation_count") or 0)
    if raw_violation_count > 0 and not reconciliation_authoritative:
        blockers.append(f"source_cash_balance_violations={raw_violation_count}")
    if not actions_available and int(source_cash_report.get("no_match_count") or 0) > 0:
        blockers.append(f"source_cash_no_match_count={int(source_cash_report.get('no_match_count') or 0)}")
    if not actions_available and int(source_cash_report.get("split_scope_missing_property_count") or 0) > 0:
        blockers.append(
            f"source_cash_split_scope_missing_property_count={int(source_cash_report.get('split_scope_missing_property_count') or 0)}"
        )
    if active_action_count > 0:
        blockers.append(f"source_cash_active_reconciliation_actions={active_action_count}")
    if actions_stale:
        blockers.append("source_cash_reconciliation_actions_stale")
    if digest_mismatch:
        blockers.append("source_cash_reconciliation_actions_source_digest_mismatch")
    return blockers


def cf_report_blockers(cf_report: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    status = str(cf_report.get("status") or "")
    if status and status != "ok":
        blockers.append(f"cf_balance_sheet_status={status}")
    if cf_report.get("workbook_audit_enabled") is False:
        blockers.append("cf_balance_sheet_workbook_audit_disabled")
    summaries = cf_report.get("summaries") if isinstance(cf_report.get("summaries"), list) else []
    if any(
        isinstance(summary, dict)
        and summary.get("workbook_audit_status") == "skipped_live_workbook_io_disabled"
        for summary in summaries
    ) and "cf_balance_sheet_workbook_audit_disabled" not in blockers:
        blockers.append("cf_balance_sheet_workbook_audit_disabled")
    issue_count = int(cf_report.get("issue_count") or 0)
    if issue_count > 0:
        blockers.append(f"cf_balance_sheet_issue_count={issue_count}")
    return blockers


def untagged_review_blockers(report: dict[str, Any]) -> list[str]:
    status = str(report.get("status") or "").strip().lower()
    if status in {"missing", "unreadable"}:
        return [f"cf_untagged_review_status={status}"]
    blockers: list[str] = []
    raw_row_count = int(report.get("untagged_row_count") or 0)
    raw_review_required_count = int(report.get("review_required_count") or 0)
    row_count = int(report.get("effective_untagged_row_count", raw_row_count) or 0)
    review_required_count = int(report.get("effective_review_required_count", raw_review_required_count) or 0)
    if row_count > 0:
        blockers.append(f"cf_untagged_row_count={row_count}")
    if review_required_count > 0:
        blockers.append(f"cf_untagged_review_required_count={review_required_count}")
    if status not in {"", "ok"}:
        blockers.append(f"cf_untagged_review_status={status}")
    return blockers


def monthly_accrual_blockers(
    accrual_report: dict[str, Any],
    inactive_rows: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    blockers: list[str] = []
    status = str(accrual_report.get("status") or "")
    if status != "ok":
        blockers.append(f"monthly_accruals_status={status or 'missing'}")
    missing_accruals = accrual_report.get("missing_accruals") if isinstance(accrual_report.get("missing_accruals"), list) else []
    active_missing_accrual_amounts = []
    for item in missing_accruals:
        if not isinstance(item, dict):
            continue
        property_name = str(item.get("property") or item.get("property_name") or "").strip()
        if inactive_rows and inactive_yhome_match(property_name, inactive_rows):
            continue
        active_missing_accrual_amounts.append(abs(parse_money(item.get("amount")) or 0.0))
    missing_count = int(accrual_report.get("missing_count") or 0)
    active_missing_count = len(active_missing_accrual_amounts) if missing_accruals else missing_count
    missing_count_blocks = bool(
        active_missing_count > 0
        and (
            status != "ok"
            or any(amount > 0 for amount in active_missing_accrual_amounts)
            or not missing_accruals
        )
    )
    if missing_count_blocks:
        blockers.append(f"monthly_accruals_missing_count={active_missing_count}")
    active_without_templates = (
        accrual_report.get("active_without_accrual_templates")
        if isinstance(accrual_report.get("active_without_accrual_templates"), list)
        else []
    )
    effective_active_without_template_count = 0
    for item in active_without_templates:
        if not isinstance(item, dict):
            continue
        property_name = str(
            item.get("property")
            or item.get("property_name")
            or item.get("full_address")
            or ""
        ).strip()
        if inactive_rows and inactive_yhome_match(property_name, inactive_rows):
            continue
        effective_active_without_template_count += 1
    if not active_without_templates:
        effective_active_without_template_count = int(
            accrual_report.get("active_without_accrual_template_count") or 0
        )
    for field in (
        "amount_mismatch_count",
        "blocked_first_day_pm_fee_count",
        "active_without_template_count",
        "active_without_templates_count",
        "unapproved_pm_fee_basis_gap_count",
        "blocking_gap_action_count",
    ):
        value = int(accrual_report.get(field) or 0)
        if value > 0:
            blockers.append(f"monthly_accruals_{field}={value}")
    if effective_active_without_template_count > 0:
        blockers.append(
            "monthly_accruals_active_without_accrual_template_count="
            f"{effective_active_without_template_count}"
        )
    gap_approvals = accrual_report.get("gap_approvals")
    missing_fixed_count = int(accrual_report.get("missing_fixed_accrual_coverage_count") or 0)
    reviewed_gap_coverage = (
        status == "ok"
        and int(accrual_report.get("unapproved_pm_fee_basis_gap_count") or 0) == 0
        and int(accrual_report.get("blocking_gap_action_count") or 0) == 0
        and isinstance(gap_approvals, dict)
        and str(gap_approvals.get("status") or "").strip() == "ok"
        and int(gap_approvals.get("issue_count") or 0) == 0
    )
    if missing_fixed_count > 0 and not reviewed_gap_coverage:
        blockers.append(f"monthly_accruals_missing_fixed_accrual_coverage_count={missing_fixed_count}")
    if isinstance(gap_approvals, dict):
        gap_status = str(gap_approvals.get("status") or "").strip()
        gap_issue_count = int(gap_approvals.get("issue_count") or 0)
        if gap_status and gap_status != "ok":
            blockers.append(f"monthly_accruals_gap_approval_status={gap_status}")
        if gap_issue_count > 0:
            blockers.append(f"monthly_accruals_gap_approval_issue_count={gap_issue_count}")
    return blockers


def monthly_accrual_live_plan_for_month(month: str | None) -> Path:
    text = str(month or "").strip()
    match = re.fullmatch(r"(\d{4})-(\d{2})", text)
    if not match:
        return DEFAULT_MONTHLY_ACCRUALS_LIVE_PLAN
    return ROOT / "reports" / f"baselane_monthly_accruals_{match.group(1)}{match.group(2)}.live-plan.json"


def monthly_accrual_live_plan_blockers(
    live_plan: dict[str, Any],
    login_wait_report: dict[str, Any] | None = None,
    live_verify_report: dict[str, Any] | None = None,
) -> list[str]:
    if live_plan.get("status") in {"missing", "unreadable"}:
        return []
    blockers: list[str] = []
    status = str(live_plan.get("status") or "")
    auth_or_cdp_blocked = live_plan.get("auth_blocked") is True or live_plan.get("cdp_blocked") is True
    if status != "ok" and not auth_or_cdp_blocked:
        blockers.append(f"monthly_accruals_live_plan_status={status or 'missing'}")
    login_wait = login_wait_report if isinstance(login_wait_report, dict) else {}
    if live_plan.get("auth_blocked") is True:
        reason = str(login_wait.get("reason") or live_plan.get("reason") or "auth_blocked").strip()
        blockers.append(f"monthly_accruals_live_plan_auth_blocked={reason}")
    if live_plan.get("cdp_blocked") is True:
        blockers.append("monthly_accruals_live_plan_cdp_blocked=true")
    live_verify_matches = monthly_accrual_live_verify_matches(live_plan, live_verify_report)
    live_verify_clean = monthly_accrual_live_verify_clean(live_plan, live_verify_report)
    # A successful plan is newer, direct authenticated evidence than a separate
    # login-wait artifact, which may retain a prior CAPTCHA observation.
    if status != "ok" and login_wait.get("recaptcha_present") is True and not live_verify_matches:
        blockers.append("monthly_accruals_live_plan_recaptcha_required=true")
    for field in ("issue_count", "create_count", "update_count"):
        value = int(live_plan.get(field) or 0)
        if value > 0 and not live_verify_clean:
            blockers.append(f"monthly_accruals_live_plan_{field}={value}")
    return blockers


def monthly_accrual_live_verify_matches(
    live_plan: dict[str, Any],
    live_verify_report: dict[str, Any] | None = None,
) -> bool:
    live_verify = live_verify_report if isinstance(live_verify_report, dict) else {}
    return (
        live_plan.get("status") == "ok"
        and live_verify.get("status") == "ok"
        and int(live_verify.get("issue_count") or 0) == 0
        and str(live_verify.get("target_digest") or "") != ""
        and str(live_verify.get("target_digest") or "") == str(live_plan.get("target_digest") or "")
    )


def monthly_accrual_live_verify_clean(
    live_plan: dict[str, Any],
    live_verify_report: dict[str, Any] | None = None,
) -> bool:
    live_verify = live_verify_report if isinstance(live_verify_report, dict) else {}
    target_count = int(live_verify.get("target_count") or 0)
    return (
        monthly_accrual_live_verify_matches(live_plan, live_verify_report)
        and int(live_verify.get("create_count") or 0) == 0
        and int(live_verify.get("update_count") or 0) == 0
        and target_count > 0
        and int(live_verify.get("skip_count") or 0) == target_count
    )


def monthly_accrual_amount_mismatch_details(accrual_report: dict[str, Any]) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for item in accrual_report.get("amount_mismatches") or accrual_report.get("amount_mismatch_samples") or []:
        if not isinstance(item, dict):
            continue
        details.append(
            {
                "property": item.get("property"),
                "month": item.get("month"),
                "kind": item.get("kind"),
                "current_row_amount": parse_money(item.get("current_row_amount")),
                "current_marker_amount": parse_money(item.get("current_marker_amount")),
                "expected_amount": parse_money(item.get("expected_amount")),
                "stale_pm_rule": item.get("stale_pm_rule") is True,
                "legacy_dao_label": item.get("legacy_dao_label") is True,
            }
        )
    return details


def monthly_accrual_live_plan_update_details(live_plan: dict[str, Any]) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for action in live_plan.get("actions") or []:
        if not isinstance(action, dict) or action.get("action") != "update":
            continue
        values = action.get("values") if isinstance(action.get("values"), dict) else {}
        marker_key = str(action.get("marker_key") or "").strip()
        parts = marker_key.split("|")
        details.append(
            {
                "id": action.get("id"),
                "marker_key": marker_key,
                "kind": parts[1] if len(parts) > 1 else None,
                "property": parts[2] if len(parts) > 2 else values.get("merchantName"),
                "month": parts[3] if len(parts) > 3 else None,
                "amount": parse_money(values.get("amount")),
                "absolute_amount": abs(parse_money(values.get("amount")) or 0.0),
                "date": values.get("date"),
                "merchant_name": values.get("merchantName"),
                "note": values.get("note"),
                "property_id": values.get("propertyId"),
                "tag_id": values.get("tagId"),
            }
        )
    return details


def suppress_local_monthly_accrual_gap_blockers(
    blockers: list[str],
    accrual_report: dict[str, Any],
    live_plan: dict[str, Any],
    live_verify_report: dict[str, Any] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    live_verify_clean = monthly_accrual_live_verify_clean(live_plan, live_verify_report)
    live_plan_usable = live_verify_clean or (
        live_plan.get("status") == "ok"
        and int(live_plan.get("issue_count") or 0) == 0
        and int(live_plan.get("create_count") or 0) == 0
        and int(live_plan.get("target_count") or 0) > 0
    )
    gap_approvals = accrual_report.get("gap_approvals") if isinstance(accrual_report.get("gap_approvals"), dict) else {}
    approved_gap_review = (
        int(accrual_report.get("unapproved_pm_fee_basis_gap_count") or 0) == 0
        and str(gap_approvals.get("status") or "").strip() == "ok"
        and int(gap_approvals.get("issue_count") or 0) == 0
    )
    suppressible_prefixes = (
        "monthly_accruals_status=review",
        "monthly_accruals_missing_count=",
        "monthly_accruals_blocking_gap_action_count=",
        "monthly_accruals_missing_fixed_accrual_coverage_count=",
    )
    suppressed: list[str] = []
    kept: list[str] = []
    for blocker in blockers:
        if live_plan_usable and approved_gap_review and blocker.startswith(suppressible_prefixes):
            suppressed.append(blocker)
        else:
            kept.append(blocker)
    return kept, {
        "status": "ok" if suppressed else "not_applicable",
        "live_plan_usable": live_plan_usable,
        "live_verify_clean": live_verify_clean,
        "approved_gap_review": approved_gap_review,
        "suppressed_blockers": suppressed,
        "suppressed_blocker_count": len(suppressed),
        "policy": (
            "A current ok dry-run live accrual plan with zero creates/issues proves locally missing PM coverage is stale; "
            "updates, creates, issues, amount mismatches, and unapproved gaps still block final transfer amounts."
        ),
    }


def monthly_accrual_coverage_details(accrual_report: dict[str, Any]) -> list[dict[str, Any]]:
    coverage: dict[str, dict[str, Any]] = {}
    for item in accrual_report.get("missing_fixed_accrual_coverage") or []:
        if not isinstance(item, dict):
            continue
        property_name = str(item.get("property") or "").strip()
        if not property_name:
            continue
        key = normalize_property_name(property_name)
        detail = coverage.setdefault(
            key,
            {"property": property_name, "missing_fixed_accrual_kinds": [], "missing_fixed_accrual_keys": []},
        )
        kind = str(item.get("kind") or "").strip()
        if kind and kind not in detail["missing_fixed_accrual_kinds"]:
            detail["missing_fixed_accrual_kinds"].append(kind)
        missing_key = str(item.get("key") or "").strip()
        if missing_key:
            detail["missing_fixed_accrual_keys"].append(missing_key)
    for item in accrual_report.get("pm_fee_basis_gaps") or []:
        if not isinstance(item, dict):
            continue
        property_name = str(item.get("property") or "").strip()
        if not property_name:
            continue
        key = normalize_property_name(property_name)
        detail = coverage.setdefault(
            key,
            {"property": property_name, "missing_fixed_accrual_kinds": [], "missing_fixed_accrual_keys": []},
        )
        detail["pm_fee_basis_gap"] = True
        detail["pm_fee_basis_gap_current_month_gross_rent"] = parse_money(item.get("current_month_gross_rent"))
        detail["pm_fee_basis_gap_previous_month_gross_rent"] = parse_money(item.get("previous_month_gross_rent"))
    return list(coverage.values())


def monthly_accrual_gap_approval_details(accrual_report: dict[str, Any]) -> list[dict[str, Any]]:
    gaps_by_key = {
        str(item.get("key") or ""): item
        for item in (accrual_report.get("pm_fee_basis_gaps") or [])
        if isinstance(item, dict) and str(item.get("key") or "")
    }
    queue_by_key = {
        str(item.get("key") or ""): item
        for item in (accrual_report.get("gap_action_queue") or [])
        if isinstance(item, dict) and str(item.get("key") or "")
    }
    issues_by_key: dict[str, list[str]] = {}
    gap_approvals = accrual_report.get("gap_approvals") if isinstance(accrual_report.get("gap_approvals"), dict) else {}
    for issue in gap_approvals.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        key = str(issue.get("key") or "")
        code = str(issue.get("code") or "")
        if key and code:
            issues_by_key.setdefault(key, []).append(code)
    details: list[dict[str, Any]] = []
    for key in sorted(set(gaps_by_key) | set(queue_by_key) | set(issues_by_key)):
        gap = gaps_by_key.get(key, {})
        queue_item = queue_by_key.get(key, {})
        details.append(
            {
                "key": key,
                "property": gap.get("property") or queue_item.get("property"),
                "kind": gap.get("kind") or queue_item.get("kind"),
                "month": gap.get("month") or queue_item.get("month"),
                "action": queue_item.get("action") or "verify_missing_rent_or_approve_zero_pm",
                "severity": queue_item.get("severity"),
                "current_month_gross_rent": parse_money(
                    gap.get("current_month_gross_rent", queue_item.get("current_month_gross_rent"))
                ),
                "previous_month": gap.get("previous_month") or queue_item.get("previous_month"),
                "previous_month_gross_rent": parse_money(
                    gap.get("previous_month_gross_rent", queue_item.get("previous_month_gross_rent"))
                ),
                "review_action": gap.get("review_action") or queue_item.get("review_action"),
                "approval_issue_codes": issues_by_key.get(key, []),
                "approval_decision": queue_item.get("approval_decision"),
                "approval_reviewed_at": queue_item.get("approval_reviewed_at"),
                "approval_effect": (
                    "waive_pm_accrual_for_month_only_no_cash_transfer"
                    if (queue_item.get("action") or "") == "verify_missing_rent_or_approve_zero_pm"
                    else None
                ),
            }
        )
    return details


def matched_monthly_accrual_coverage_detail(property_name: str, details: list[dict[str, Any]]) -> dict[str, Any]:
    for detail in details:
        for candidate in [detail.get("property"), *(detail.get("missing_fixed_accrual_keys") or [])]:
            if names_match(property_name, candidate):
                return detail
    return {}


def monthly_accrual_append_audit_acceptance(append_audit: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    if append_audit.get("status") in {"ok", "superseded_by_guarded_live_baselane_sync", "", None}:
        return {
            "accepted": False,
            "status": "not_required",
            "issues": [],
            "path": decision.get("path"),
            "decision": decision.get("decision"),
            "reviewed_at": decision.get("reviewed_at"),
            "reviewed_by": decision.get("reviewed_by"),
        }
    issues: list[str] = []
    if decision.get("decision") != "accept_current_aops_overlay":
        issues.append("decision_not_accept_current_aops_overlay")
    if decision.get("reviewed") is not True:
        issues.append("decision_not_reviewed")
    if not str(decision.get("reviewed_at") or "").strip():
        issues.append("reviewed_at_missing")
    if not str(decision.get("reviewed_by") or "").strip():
        issues.append("reviewed_by_missing")
    for field in ("current_sha256", "baseline_sha256", "added_aops_count", "added_non_aops_count", "removed_count"):
        if decision.get(field) != append_audit.get(field):
            issues.append(f"{field}_mismatch")
    accepted = not issues
    return {
        "accepted": accepted,
        "status": "accepted" if accepted else "review",
        "issues": issues,
        "path": decision.get("path"),
        "decision": decision.get("decision"),
        "reviewed_at": decision.get("reviewed_at"),
        "reviewed_by": decision.get("reviewed_by"),
    }


def monthly_accrual_append_audit_blockers(append_audit: dict[str, Any], decision: dict[str, Any] | None = None) -> list[str]:
    status = str(append_audit.get("status") or "").strip()
    if status in {"", "missing", "superseded_by_guarded_live_baselane_sync"}:
        return []
    acceptance = monthly_accrual_append_audit_acceptance(append_audit, decision or {})
    if acceptance["accepted"]:
        return []
    blockers: list[str] = []
    added_aops_count = int(append_audit.get("added_aops_count") or 0)
    added_non_aops_count = int(append_audit.get("added_non_aops_count") or 0)
    removed_count = int(append_audit.get("removed_count") or 0)
    if status != "ok":
        blockers.append(f"monthly_accruals_append_audit_status={status}")
    if added_aops_count > 0:
        blockers.append(f"monthly_accruals_append_audit_added_aops_count={added_aops_count}")
    if added_non_aops_count > 0:
        blockers.append(f"monthly_accruals_append_audit_added_non_aops_count={added_non_aops_count}")
    if removed_count > 0:
        blockers.append(f"monthly_accruals_append_audit_removed_count={removed_count}")
    if decision:
        blockers.extend(f"monthly_accruals_append_audit_decision_{issue}" for issue in acceptance["issues"][:5])
    return blockers


def coownership_blockers(path: Path) -> dict[str, list[str]]:
    payload = read_json(path)
    blockers: dict[str, list[str]] = {}
    for record in payload.get("records") or []:
        if not isinstance(record, dict) or record.get("status") == "ok":
            continue
        property_name = str(record.get("property") or "").strip()
        if not property_name:
            continue
        blockers[normalize_property_name(property_name)] = [
            str(issue) for issue in (record.get("issues") or ["coownership_gl_policy_validation_blocked"])
        ]
    return blockers


def coownership_review_details(path: Path) -> list[dict[str, Any]]:
    payload = read_json(path)
    details: list[dict[str, Any]] = []
    for record in payload.get("records") or []:
        if not isinstance(record, dict) or record.get("status") == "ok":
            continue
        property_name = str(record.get("property") or "").strip()
        if not property_name:
            continue
        prepared = (
            record.get("prepared_upstream_retag_evidence")
            if isinstance(record.get("prepared_upstream_retag_evidence"), dict)
            else {}
        )
        details.append(
            {
                "property": property_name,
                "property_key": normalize_property_name(property_name),
                "issues": [str(issue) for issue in (record.get("issues") or [])],
                "next_action": record.get("next_action"),
                "pre_launch_row_count": int(record.get("pre_launch_row_count") or 0),
                "pre_launch_amount_sum": parse_money(record.get("pre_launch_amount_sum")),
                "prepared_retag_ready_count": int(prepared.get("ready_count") or 0) if prepared else 0,
                "prepared_retag_payload_digest": prepared.get("payload_digest") if prepared else None,
                "protected_closing_row_count": (
                    int(prepared.get("protected_closing_row_count") or 0) if prepared else 0
                ),
                "protected_closing_row_review_status": (
                    prepared.get("protected_closing_row_review_status") if prepared else None
                ),
                "protected_closing_row_reviewed_count": (
                    int(prepared.get("protected_closing_row_reviewed_count") or 0) if prepared else 0
                ),
                "protected_closing_row_review_required_count": (
                    int(prepared.get("protected_closing_row_review_required_count") or 0) if prepared else 0
                ),
                "protected_closing_row_review_blockers": (
                    prepared.get("protected_closing_row_review_blockers") or [] if prepared else []
                ),
                "guarded_apply_command": prepared.get("guarded_apply_command") if prepared else None,
                "protected_review_csv": prepared.get("protected_review_csv") if prepared else None,
                "protected_review_import_command_file": (
                    prepared.get("protected_review_import_command_file") if prepared else None
                ),
                "source_report": prepared.get("source_report") if prepared else str(path),
            }
        )
    return details


def property_cash_review_blockers(paths: list[Path]) -> dict[str, list[str]]:
    blockers: dict[str, list[str]] = {}
    for path in paths:
        payload = read_json(path)
        if payload.get("status") == "missing":
            continue
        property_name = str(payload.get("property") or "").strip()
        if not property_name:
            continue
        review_count = int(payload.get("classification_review_count") or 0)
        findings = payload.get("preliminary_findings") or []
        if review_count <= 0 and not findings:
            continue
        blockers[normalize_property_name(property_name)] = ["property_cash_alignment_review_required"]
    return blockers


def property_cash_review_details(paths: list[Path]) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for path in paths:
        payload = read_json(path)
        if payload.get("status") == "missing":
            continue
        property_name = str(payload.get("property") or "").strip()
        if not property_name:
            continue
        dedupe_key = (normalize_property_name(property_name), str(path.resolve() if path.exists() else path))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        review_count = int(payload.get("classification_review_count") or 0)
        findings = [str(item) for item in payload.get("preliminary_findings") or [] if str(item or "").strip()]
        if review_count <= 0 and not findings:
            continue
        source_clean_status = payload.get("source_clean_status")
        decision_validation_status = payload.get("decision_validation_status")
        decision_validation_effective_status = (
            payload.get("decision_validation_effective_status")
            or (source_clean_status if source_clean_status and decision_validation_status == "ok" else decision_validation_status)
        )
        details.append(
            {
                "property": property_name,
                "property_key": normalize_property_name(property_name),
                "status": payload.get("status"),
                "source_clean_status": source_clean_status,
                "decision_validation_status": decision_validation_status,
                "decision_validation_effective_status": decision_validation_effective_status,
                "report": str(path),
                "classification_review_count": review_count,
                "classification_review_sum": parse_money(payload.get("classification_review_sum")),
                "upstream_retag_required_remaining_count": int(
                    payload.get("upstream_retag_required_remaining_count")
                    or (
                        (payload.get("source_clean_gate") or {}).get("upstream_retag_required_remaining_count")
                        if isinstance(payload.get("source_clean_gate"), dict)
                        else 0
                    )
                    or 0
                ),
                "upstream_retag_required_remaining_sum": parse_money(
                    payload.get("upstream_retag_required_remaining_sum")
                    or (
                        (payload.get("source_clean_gate") or {}).get("upstream_retag_required_remaining_sum")
                        if isinstance(payload.get("source_clean_gate"), dict)
                        else 0.0
                    )
                ),
                "risk_tag_counts": payload.get("risk_tag_counts") if isinstance(payload.get("risk_tag_counts"), dict) else {},
                "risk_tag_amount_sums": (
                    payload.get("risk_tag_amount_sums") if isinstance(payload.get("risk_tag_amount_sums"), dict) else {}
                ),
                "review_action_buckets": (
                    payload.get("review_action_buckets") if isinstance(payload.get("review_action_buckets"), list) else []
                ),
                "review_priority_buckets": (
                    payload.get("review_priority_buckets") if isinstance(payload.get("review_priority_buckets"), list) else []
                ),
                "net_cash_exposure_review": (
                    payload.get("net_cash_exposure_review")
                    if isinstance(payload.get("net_cash_exposure_review"), dict)
                    else {}
                ),
                "review_progress": (
                    payload.get("review_progress") if isinstance(payload.get("review_progress"), dict) else {}
                ),
                "next_review_groups": (
                    payload.get("review_progress", {}).get("next_review_groups", [])
                    if isinstance(payload.get("review_progress"), dict)
                    and isinstance(payload.get("review_progress", {}).get("next_review_groups"), list)
                    else []
                ),
                "reviewed_template_reviewed_group_count": int(payload.get("reviewed_template_reviewed_group_count") or 0),
                "reviewed_template_source_group_count": int(payload.get("reviewed_template_source_group_count") or 0),
                "reviewed_template_unreviewed_group_count": int(payload.get("reviewed_template_unreviewed_group_count") or 0),
                "reviewed_template_high_priority_unreviewed_group_count": int(
                    payload.get("reviewed_template_high_priority_unreviewed_group_count") or 0
                ),
                "reviewed_template_unreviewed_absolute_amount_sum": parse_money(
                    payload.get("reviewed_template_unreviewed_absolute_amount_sum")
                ),
                "balanced_internal_transfer_group_count": int(payload.get("balanced_internal_transfer_group_count") or 0),
                "balanced_internal_transfer_row_count": int(payload.get("balanced_internal_transfer_row_count") or 0),
                "top_review_accounts": (
                    payload.get("top_review_accounts") if isinstance(payload.get("top_review_accounts"), list) else []
                ),
                "top_review_categories": (
                    payload.get("top_review_categories") if isinstance(payload.get("top_review_categories"), list) else []
                ),
                "top_review_months": (
                    payload.get("top_review_months") if isinstance(payload.get("top_review_months"), list) else []
                ),
                "review_samples_by_risk_tag": (
                    payload.get("review_samples_by_risk_tag")
                    if isinstance(payload.get("review_samples_by_risk_tag"), dict)
                    else {}
                ),
                "reviewed_template": str(ROOT / "config" / "baselane_804_quitman_cash_alignment_reviewed_template.json"),
                "group_review_queue_csv": str(ROOT / "reports" / "baselane_804_quitman_cash_alignment_group_review_queue.csv"),
                "review_queue_csv": str(ROOT / "reports" / "baselane_804_quitman_cash_alignment_review_queue.csv"),
                "import_commands": str(
                    ROOT / "reports" / "baselane_804_quitman_cash_alignment_import_group_review.requires-explicit-approval.sh"
                ),
                "decision_template": str(ROOT / "reports" / "baselane_804_quitman_cash_alignment_decision_template.json"),
                "proposed_decisions": str(ROOT / "reports" / "baselane_804_quitman_cash_alignment_proposed_decisions.json"),
                "decision_validation": str(ROOT / "reports" / "baselane_804_quitman_cash_alignment_decision_validation.json"),
                "recommended_transfer_instruction": payload.get("recommended_transfer_instruction"),
                "preliminary_findings": findings[:10],
            }
        )
    return sorted(details, key=lambda item: item["property_key"])


def matched_property_cash_review_detail(property_name: str, details: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in details:
        if names_match(property_name, item.get("property_key")) or names_match(property_name, item.get("property")):
            return item
    return None


def property_matches_any_row(property_name: str, rows: list[dict[str, Any]]) -> bool:
    return any(names_match(property_name, row.get("property")) for row in rows)


def scoped_property_cash_review_blockers(
    policy_blockers: dict[str, list[str]],
    rows: list[dict[str, Any]],
) -> list[str]:
    return [
        f"property_cash_review:{property_key}:{reason}"
        for property_key, reasons in sorted(policy_blockers.items())
        if property_matches_any_row(property_key, rows)
        for reason in reasons
    ]


def scoped_property_cash_review_details(
    details: list[dict[str, Any]],
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        detail
        for detail in details
        if property_matches_any_row(str(detail.get("property_key") or detail.get("property") or ""), rows)
    ]


def no_dao_mortgage_policy_applies(property_name: str, property_path: str) -> bool:
    return is_no_dao_mortgage_property(f"{property_name} {property_path}")


def cf_reflection(cf_summary: dict[str, Any] | None, eco_cash: float | None) -> tuple[bool, str, float | None, str | None]:
    if not cf_summary:
        return False, "cf_summary_missing", None, None
    if cf_summary.get("eco_balance_semantics"):
        expected = parse_money(cf_summary.get("eco_operating_cash_expected"))
        actual = parse_money(cf_summary.get("eco_operating_cash_actual"))
        cell = cf_summary.get("eco_operating_cash_cell")
        if expected is None or actual is None:
            return False, "cf_historical_eco_cash_missing", actual, cell
        if abs(round(expected - actual, 2)) > CONFLICT_THRESHOLD:
            return False, "cf_historical_eco_cash_mismatch", actual, cell
        return True, "ok_historical_cf_balance", actual, cell
    if cf_summary.get("workbook_audit_status") == "skipped_live_workbook_io_disabled":
        expected = parse_money(cf_summary.get("eco_operating_cash_expected"))
        if eco_cash is None:
            return False, "eco_source_missing", expected, None
        if expected is None:
            return False, "cf_eco_cash_expected_missing", expected, None
        if abs(round(expected - eco_cash, 2)) > CONFLICT_THRESHOLD:
            return False, "cf_eco_cash_expected_mismatch", expected, None
        return True, "ok_source_expected_workbook_io_skipped", expected, None
    actual = parse_money(cf_summary.get("eco_operating_cash_actual"))
    cell = cf_summary.get("eco_operating_cash_cell")
    if eco_cash is None:
        return False, "eco_source_missing", actual, cell
    if actual is None:
        return False, "cf_eco_cash_actual_missing", actual, cell
    if abs(round(actual - eco_cash, 2)) > CONFLICT_THRESHOLD:
        return False, "cf_eco_cash_mismatch", actual, cell
    return True, "ok", actual, cell


def bank_transfer_instruction(
    *,
    action: str,
    surplus: float | None,
    shortfall: float | None,
    hold_reasons: list[str],
) -> dict[str, Any]:
    if action == "skip_inactive":
        return {
            "bank_transfer_action": "skip_inactive",
            "bank_transfer_amount": None,
            "bank_transfer_direction": "none",
            "next_action": "Skip inactive/sold property.",
        }
    if action == "send_to_lofty":
        return {
            "bank_transfer_action": "send_to_lofty",
            "bank_transfer_amount": surplus,
            "bank_transfer_direction": "ECO/source account -> Lofty account",
            "next_action": "Transfer ECO-held cash above the combined ECO + Lofty OR floor to Lofty.",
        }
    if shortfall and shortfall > 0:
        return {
            "bank_transfer_action": "top_up_eco",
            "bank_transfer_amount": shortfall,
            "bank_transfer_direction": "Funding source -> DAO ECO/source account or Lofty OR",
            "next_action": "Top up combined ECO-held spendable cash plus Lofty OR to the required floor.",
        }
    if "property_cash_alignment_review_required" in hold_reasons:
        return {
            "bank_transfer_action": "review_before_transfer",
            "bank_transfer_amount": None,
            "bank_transfer_direction": "hold",
            "next_action": "Complete bank-vs-GL alignment review before moving cash.",
        }
    coownership_reasons = [reason for reason in hold_reasons if reason.startswith("coownership_gl_policy:")]
    if coownership_reasons:
        return {
            "bank_transfer_action": "fix_source_tagging_before_transfer",
            "bank_transfer_amount": None,
            "bank_transfer_direction": "hold",
            "next_action": "Fix upstream Baselane property tagging and rerun sync/split before moving cash.",
        }
    if action == "no_transfer":
        return {
            "bank_transfer_action": "no_transfer",
            "bank_transfer_amount": 0.0,
            "bank_transfer_direction": "none",
            "next_action": "No bank transfer required.",
        }
    return {
        "bank_transfer_action": "hold",
        "bank_transfer_amount": None,
        "bank_transfer_direction": "hold",
        "next_action": "Resolve hold reasons before moving cash.",
    }


def build_rows(
    *,
    records: list[dict[str, Any]],
    cf_index: dict[str, dict[str, Any]],
    inactive_rows: dict[str, dict[str, Any]],
    states: set[str],
    eco_minimum: float,
    global_source_blockers: list[str],
    coownership_policy_blockers: dict[str, list[str]],
    property_cash_review_policy_blockers: dict[str, list[str]],
    property_cash_review_details_by_property: list[dict[str, Any]],
    monthly_accruals_report: dict[str, Any],
    lofty_reserve_overrides: dict[str, float | None] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    accrual_coverage = monthly_accrual_coverage_details(monthly_accruals_report)
    for record in records:
        property_name = str(record.get("property_name") or record.get("input_property_name") or "").strip()
        property_path = str(record.get("property_path") or record.get("input_property_path") or "")
        state = state_from_property_path(property_path)
        if state not in states:
            continue
        inactive = inactive_yhome_match(property_name, inactive_rows)
        summary = financial_summary(record)
        eco_gl_accounting_position = parse_money(summary.get("eco_gl_column_e_sum"))
        cash_settlement_basis = parse_money(summary.get("cash_settlement_basis_sum"))
        eco_cash = parse_money(summary.get("eco_operating_cash"))
        physical_bank_cash = parse_money(summary.get("physical_bank_cash"))
        reserve_key = normalize_property_name(property_name)
        local_financials_only = canonical_reserve_property(property_name) in set(LOCAL_FINANCIALS_ONLY_PROPERTIES)
        lofty_operating_cash_source = (
            lofty_reserve_overrides[reserve_key]
            if lofty_reserve_overrides is not None and reserve_key in lofty_reserve_overrides
            else parse_money(summary.get("lofty_curr_maintenance_reserve"))
        )
        lofty_operating_cash_missing = lofty_operating_cash_source is None
        lofty_operating_cash = 0.0 if lofty_operating_cash_missing else lofty_operating_cash_source
        row_count = summary.get("eco_gl_column_e_row_count")
        source_status = str(summary.get("eco_gl_column_e_status") or "")
        source_scope = str(summary.get("eco_gl_column_e_scope") or "").strip() or None
        source_mode = summary.get("eco_gl_column_e_source_mode")
        as_of_month = str(summary.get("as_of_month") or "").strip() or None
        as_of_month_sum = parse_money(summary.get("eco_gl_column_e_sum_as_of_month"))
        as_of_month_row_count = summary.get("eco_gl_column_e_row_count_as_of_month")
        cf_summary = cf_index.get(normalize_property_name(property_name))
        cf_reflected, cf_status, cf_actual, cf_cell = cf_reflection(cf_summary, eco_gl_accounting_position)
        no_dao_mortgage_policy = no_dao_mortgage_policy_applies(property_name, property_path)
        no_dao_mortgage_source_unresolved = no_dao_mortgage_policy and bool(global_source_blockers)
        no_dao_mortgage_liability_review_required = no_dao_mortgage_source_unresolved
        effective_lofty_operating_cash = (
            0.0
            if local_financials_only and lofty_operating_cash_missing
            else lofty_operating_cash_source
        )
        reserve_position = (
            combined_reserve_position(
                eco_cash,
                effective_lofty_operating_cash,
                eco_minimum,
            )
            if eco_cash is not None and effective_lofty_operating_cash is not None
            else None
        )
        total_operating_cash = (
            float(reserve_position["combined_reserve_liquidity"])
            if reserve_position is not None
            else None
        )
        surplus = (
            float(reserve_position["sendable_eco_cash"])
            if reserve_position is not None
            else None
        )
        provisional_send = surplus
        if no_dao_mortgage_liability_review_required:
            provisional_send = None
        shortfall = (
            float(reserve_position["combined_shortfall_to_floor"])
            if reserve_position is not None
            else None
        )
        hold_reasons: list[str] = []
        if inactive:
            inactive_source = str(inactive.get("source") or "yhome_transition_reconciliation").strip()
            hold_reasons.append(
                "inactive_yhome_status"
                if inactive_source == "yhome_transition_reconciliation"
                else f"inactive_{inactive_source}"
            )
        if source_status != "ok":
            hold_reasons.append("eco_gl_column_e_source_not_ok")
        if eco_cash is None or summary.get("eco_operating_cash_status") != "ok":
            hold_reasons.append("eco_operating_cash_authority_not_ok")
        if source_scope and source_scope != "all_property_split_rows":
            hold_reasons.append("eco_gl_column_e_scope_not_all_rows")
        if lofty_operating_cash_missing and not local_financials_only:
            hold_reasons.append("lofty_curr_maintenance_reserve_source_not_ok")
        hold_reasons.extend(global_source_blockers)
        if no_dao_mortgage_source_unresolved:
            hold_reasons.append("no_dao_mortgage_responsibility_source_unresolved")
        if no_dao_mortgage_liability_review_required:
            hold_reasons.append("no_dao_mortgage_responsibility_liability_review_required")
        if not cf_reflected:
            hold_reasons.append(cf_status)
        for blocker_key, reasons in coownership_policy_blockers.items():
            if names_match(property_name, blocker_key):
                hold_reasons.extend(f"coownership_gl_policy:{reason}" for reason in reasons)
        for blocker_key, reasons in property_cash_review_policy_blockers.items():
            if names_match(property_name, blocker_key):
                hold_reasons.extend(reasons)
        property_cash_detail = matched_property_cash_review_detail(property_name, property_cash_review_details_by_property)
        property_cash_exposure = (
            property_cash_detail.get("net_cash_exposure_review")
            if isinstance(property_cash_detail, dict) and isinstance(property_cash_detail.get("net_cash_exposure_review"), dict)
            else {}
        )
        property_cash_review_progress = (
            property_cash_detail.get("review_progress")
            if isinstance(property_cash_detail, dict) and isinstance(property_cash_detail.get("review_progress"), dict)
            else {}
        )
        accrual_coverage_detail = matched_monthly_accrual_coverage_detail(property_name, accrual_coverage)
        if shortfall and shortfall > 0:
            hold_reasons.append("combined_eco_and_lofty_reserve_below_minimum")
        if inactive:
            action = "skip_inactive"
        elif hold_reasons:
            action = "hold"
        elif surplus and surplus > 0:
            action = "send_to_lofty"
        else:
            action = "no_transfer"
        transfer_instruction = bank_transfer_instruction(
            action=action,
            surplus=surplus,
            shortfall=shortfall,
            hold_reasons=hold_reasons,
        )
        rows.append(
            {
                "property": property_name,
                "state": state,
                "property_path": property_path,
                "eco_operating_cash": eco_cash,
                "eco_operating_cash_formatted": money(eco_cash),
                "eco_operating_cash_balance_basis": summary.get("eco_operating_cash_source_mode"),
                "eco_operating_cash_as_of_date": summary.get("eco_operating_cash_as_of_date"),
                "eco_operating_cash_balance_scope": summary.get("eco_operating_cash_balance_scope"),
                "physical_bank_cash": physical_bank_cash,
                "physical_bank_cash_formatted": money(physical_bank_cash),
                "physical_bank_cash_status": summary.get("physical_bank_cash_status"),
                "physical_bank_cash_source_mode": summary.get("physical_bank_cash_source_mode"),
                "physical_bank_cash_source": summary.get("physical_bank_cash_source"),
                "physical_bank_cash_as_of_date": summary.get("physical_bank_cash_as_of_date"),
                "bank_minus_gl_gap": (
                    round(physical_bank_cash - eco_gl_accounting_position, 2)
                    if physical_bank_cash is not None and eco_gl_accounting_position is not None
                    else None
                ),
                "eco_operating_cash_reporting_month_policy": ECO_OPERATING_CASH_REPORTING_MONTH_POLICY,
                "cash_settlement_basis_sum": cash_settlement_basis,
                "eco_gl_column_e_sum": eco_gl_accounting_position,
                "eco_gl_column_e_sum_formatted": money(eco_gl_accounting_position),
                "eco_gl_accounting_position": eco_gl_accounting_position,
                "eco_gl_accounting_position_formatted": money(eco_gl_accounting_position),
                "eco_general_ledger_sum": eco_gl_accounting_position,
                "eco_general_ledger_sum_formatted": money(eco_gl_accounting_position),
                "eco_gl_column_e_sum_as_of_month": as_of_month_sum,
                "eco_gl_column_e_row_count_as_of_month": as_of_month_row_count,
                "cash_settlement_basis_sum": cash_settlement_basis,
                "cash_settlement_basis_sum_formatted": money(cash_settlement_basis),
                "cash_settlement_basis_scope": summary.get("cash_settlement_basis_scope"),
                "non_cash_close_row_count_excluded_from_settlement": int(summary.get("non_cash_close_row_count_excluded_from_settlement") or 0),
                "non_cash_close_amount_excluded_from_settlement": parse_money(summary.get("non_cash_close_amount_excluded_from_settlement")) or 0.0,
                "lofty_curr_maintenance_reserve": None if local_financials_only and lofty_operating_cash_missing else lofty_operating_cash,
                "lofty_curr_maintenance_reserve_missing_treated_as_zero": (
                    lofty_operating_cash_missing and not local_financials_only
                ),
                "lofty_curr_maintenance_reserve_not_required": local_financials_only,
                "lofty_curr_maintenance_reserve_formatted": (
                    "not required" if local_financials_only and lofty_operating_cash_missing else money(lofty_operating_cash)
                ),
                "total_operating_cash_for_distribution_test": total_operating_cash,
                "total_operating_cash_for_distribution_test_formatted": money(total_operating_cash),
                "combined_reserve_liquidity": total_operating_cash,
                "combined_reserve_liquidity_formatted": money(total_operating_cash),
                "eco_gl_column_e_row_count": row_count,
                "eco_gl_column_e_source": summary.get("eco_gl_column_e_source"),
                "eco_gl_column_e_source_mode": source_mode,
                "eco_gl_column_e_scope": source_scope,
                "eco_gl_column_e_as_of_month": as_of_month,
                "eco_gl_column_e_reporting_month": as_of_month,
                "eco_gl_column_e_status": source_status,
                "monthly_accruals_status": monthly_accruals_report.get("status"),
                "monthly_accruals_missing_count": int(monthly_accruals_report.get("missing_count") or 0),
                "monthly_accruals_amount_mismatch_count": int(monthly_accruals_report.get("amount_mismatch_count") or 0),
                "monthly_accruals_blocked_first_day_pm_fee_count": int(monthly_accruals_report.get("blocked_first_day_pm_fee_count") or 0),
                "monthly_accruals_blocking_gap_action_count": int(monthly_accruals_report.get("blocking_gap_action_count") or 0),
                "monthly_accruals_active_without_template_count": int(
                    monthly_accruals_report.get("active_without_accrual_template_count")
                    or monthly_accruals_report.get("active_without_template_count")
                    or 0
                ),
                "monthly_accruals_missing_fixed_kinds_for_property": accrual_coverage_detail.get(
                    "missing_fixed_accrual_kinds", []
                ),
                "monthly_accruals_missing_fixed_keys_for_property": accrual_coverage_detail.get(
                    "missing_fixed_accrual_keys", []
                ),
                "monthly_accruals_pm_fee_basis_gap_for_property": accrual_coverage_detail.get("pm_fee_basis_gap") is True,
                "monthly_accruals_pm_fee_basis_gap_current_month_gross_rent": accrual_coverage_detail.get(
                    "pm_fee_basis_gap_current_month_gross_rent"
                ),
                "monthly_accruals_pm_fee_basis_gap_previous_month_gross_rent": accrual_coverage_detail.get(
                    "pm_fee_basis_gap_previous_month_gross_rent"
                ),
                "eco_minimum": round(eco_minimum, 2),
                "combined_reserve_floor": round(eco_minimum, 2),
                "eco_cash_surplus_above_minimum": surplus,
                "eco_cash_shortfall_to_minimum": shortfall,
                "combined_reserve_surplus_above_floor": (
                    float(reserve_position["combined_surplus_above_floor"])
                    if reserve_position is not None
                    else None
                ),
                "combined_reserve_shortfall_to_floor": shortfall,
                "sendable_eco_cash_above_combined_floor": surplus,
                "distribution_formula": (
                    "min(max(0, eco_operating_cash), "
                    "max(0, eco_operating_cash + lofty_operating_reserve - combined_reserve_floor))"
                ),
                "provisional_send_to_lofty_amount": provisional_send,
                "property_cash_review_required": property_cash_detail is not None,
                "property_cash_review_report": property_cash_detail.get("report") if property_cash_detail else None,
                "property_cash_review_source_clean_status": (
                    property_cash_detail.get("source_clean_status") if property_cash_detail else None
                ),
                "property_cash_review_decision_validation_status": (
                    property_cash_detail.get("decision_validation_status") if property_cash_detail else None
                ),
                "property_cash_review_decision_validation_effective_status": (
                    property_cash_detail.get("decision_validation_effective_status") if property_cash_detail else None
                ),
                "property_cash_review_classification_review_count": (
                    property_cash_detail.get("classification_review_count") if property_cash_detail else 0
                ),
                "property_cash_review_upstream_retag_required_remaining_count": (
                    property_cash_detail.get("upstream_retag_required_remaining_count") if property_cash_detail else 0
                ),
                "property_cash_review_upstream_retag_required_remaining_sum": parse_money(
                    property_cash_detail.get("upstream_retag_required_remaining_sum") if property_cash_detail else None
                ),
                "property_cash_review_high_priority_unresolved_sum": parse_money(
                    property_cash_exposure.get("high_priority_unresolved_sum")
                ),
                "property_cash_review_group_count": (
                    property_cash_review_progress.get("group_count") if property_cash_review_progress else 0
                ),
                "property_cash_review_reviewed_group_count": (
                    property_cash_review_progress.get("reviewed_group_count") if property_cash_review_progress else 0
                ),
                "property_cash_review_unreviewed_group_count": (
                    property_cash_review_progress.get("unreviewed_group_count") if property_cash_review_progress else 0
                ),
                "property_cash_review_high_priority_unreviewed_group_count": (
                    property_cash_review_progress.get("high_priority_unreviewed_group_count") if property_cash_review_progress else 0
                ),
                "property_cash_review_unreviewed_absolute_amount_sum": parse_money(
                    property_cash_review_progress.get("unreviewed_absolute_amount_sum")
                    if property_cash_review_progress
                    else None
                ),
                "property_cash_review_transfer_impact_note": (
                    "Provisional transfer is held; 804 bank-vs-GL review can change whether this cash is transferable."
                    if property_cash_detail and provisional_send
                    else None
                ),
                "recommended_send_to_lofty_amount": surplus if action == "send_to_lofty" else 0.0 if action == "no_transfer" else None,
                **transfer_instruction,
                "no_dao_mortgage_responsibility_policy": no_dao_mortgage_policy,
                "no_dao_mortgage_responsibility_liability_review_required": no_dao_mortgage_liability_review_required,
                "cf_balance_sheet_reflected": cf_reflected,
                "cf_balance_sheet_status": cf_status,
                "cf_balance_sheet_actual": cf_actual,
                "cf_balance_sheet_cell": cf_cell,
                "action": action,
                "hold_reasons": hold_reasons,
                "inactive_status": inactive.get("status") if inactive else None,
                "inactive_yhome_row": inactive.get("row") if inactive else None,
                "inactive_exclusion_source": inactive.get("source") if inactive else None,
            }
        )
    rows.sort(key=lambda item: (item["state"], item["property"].lower()))
    return rows


def write_csv_report(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "property",
        "state",
        "eco_operating_cash",
        "eco_operating_cash_balance_basis",
        "eco_gl_column_e_sum",
        "physical_bank_cash",
        "physical_bank_cash_status",
        "physical_bank_cash_source_mode",
        "physical_bank_cash_as_of_date",
        "bank_minus_gl_gap",
        "cash_settlement_basis_sum",
        "cash_settlement_basis_scope",
        "lofty_curr_maintenance_reserve",
        "total_operating_cash_for_distribution_test",
        "combined_reserve_liquidity",
        "eco_minimum",
        "combined_reserve_floor",
        "distribution_formula",
        "combined_reserve_surplus_above_floor",
        "combined_reserve_shortfall_to_floor",
        "sendable_eco_cash_above_combined_floor",
        "eco_cash_surplus_above_minimum",
        "eco_cash_shortfall_to_minimum",
        "recommended_send_to_lofty_amount",
        "provisional_send_to_lofty_amount",
        "property_cash_review_required",
        "property_cash_review_classification_review_count",
        "property_cash_review_upstream_retag_required_remaining_count",
        "property_cash_review_upstream_retag_required_remaining_sum",
        "property_cash_review_high_priority_unresolved_sum",
        "property_cash_review_transfer_impact_note",
        "bank_transfer_action",
        "bank_transfer_amount",
        "bank_transfer_direction",
        "next_action",
        "no_dao_mortgage_responsibility_policy",
        "action",
        "hold_reasons",
        "cf_balance_sheet_reflected",
        "cf_balance_sheet_status",
        "cf_balance_sheet_actual",
        "cf_balance_sheet_cell",
        "eco_gl_column_e_as_of_month",
        "eco_gl_column_e_row_count",
        "eco_gl_column_e_source",
        "monthly_accruals_status",
        "monthly_accruals_missing_count",
        "monthly_accruals_amount_mismatch_count",
        "monthly_accruals_blocked_first_day_pm_fee_count",
        "monthly_accruals_blocking_gap_action_count",
        "monthly_accruals_active_without_template_count",
        "inactive_status",
        "inactive_exclusion_source",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            payload = dict(row)
            payload["hold_reasons"] = ";".join(row.get("hold_reasons") or [])
            writer.writerow({field: payload.get(field) for field in fieldnames})


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    bank_action_amounts = report.get("bank_action_amount_totals") if isinstance(report.get("bank_action_amount_totals"), dict) else {}
    bank_action_counts = report.get("bank_action_counts") if isinstance(report.get("bank_action_counts"), dict) else {}
    lines = [
        "# Baselane Lofty Transfer Requirements",
        "",
        f"- Status: `{report['status']}`",
        f"- Source clean for final transfer amounts: `{str(report['source_clean_for_final_transfer_amounts']).lower()}`",
        f"- Combined ECO + Lofty OR reserve floor per co-ownership: `{money(report['eco_minimum'])}`",
        f"- ECO Operating Cash balance: `{money(report.get('eco_operating_cash_full_balance_total'))}`",
        f"- ECO General Ledger balance: `{money(report.get('eco_general_ledger_total'))}`",
        f"- ECO cash vs send policy: `{report.get('eco_operating_cash_vs_send_to_lofty_policy')}`",
        f"- Transfer formula: `{report['transfer_formula']}`",
        f"- Co-ownership states: `{', '.join(report['coownership_states'])}`",
        f"- Final sendable total: `{money(report['recommended_send_to_lofty_total']) if report['recommended_send_to_lofty_total'] is not None else 'held'}`",
        f"- Provisional send-to-Lofty total, excluding unresolved no-DAO-mortgage responsibility rows: `{money(report['provisional_send_to_lofty_total'])}`",
        f"- Combined ECO + Lofty OR shortfall to reserve floor: `{money(report['combined_reserve_shortfall_total'])}`",
        f"- Bank action amounts: `{bank_action_amounts}`",
        "",
        "## Bank Action Summary",
        "",
        f"- Send to Lofty: `{money(bank_action_amounts.get('send_to_lofty'))}` across `{bank_action_counts.get('send_to_lofty', 0)}` DAO(s)",
        f"- Top up ECO/source accounts: `{money(bank_action_amounts.get('top_up_eco'))}` across `{bank_action_counts.get('top_up_eco', 0)}` DAO(s)",
        f"- Held/review amount: `{money(bank_action_amounts.get('review_or_hold'))}`",
        "",
        "## Property Actions",
    ]
    for row in report["rows"]:
        send = row.get("recommended_send_to_lofty_amount")
        send_text = money(send) if send is not None else "HELD"
        transfer_amount = row.get("bank_transfer_amount")
        transfer_text = money(transfer_amount) if transfer_amount is not None else "HELD"
        reasons = ", ".join(row.get("hold_reasons") or [])
        suffix = f"; hold: {reasons}" if reasons else ""
        lines.append(
            f"- `{row['state']}` `{row['property']}`: ECO {money(row.get('eco_operating_cash'))}; "
            f"Lofty OR {money(row.get('lofty_curr_maintenance_reserve'))}; "
            f"combined reserve {money(row.get('combined_reserve_liquidity'))}; "
            f"GL source `{row.get('eco_operating_cash_balance_basis') or 'missing'}`; "
            f"physical bank cash {money(row.get('physical_bank_cash'))} "
            f"as of `{row.get('physical_bank_cash_as_of_date') or 'unknown'}`; "
            f"accounting GL {money(row.get('eco_gl_column_e_sum'))}; "
            f"bank-minus-GL gap {money(row.get('bank_minus_gl_gap'))}; "
            f"send to Lofty {send_text}; bank action `{row.get('bank_transfer_action')}` {transfer_text}; "
            f"direction `{row.get('bank_transfer_direction')}`; next `{row.get('next_action')}`; action `{row['action']}`{suffix}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def monthly_accrual_review_markdown_path(monthly_accruals_report_path: object) -> str:
    path_text = str(monthly_accruals_report_path or "")
    match = re.search(r"baselane_monthly_accruals_(\d{6})\.json$", path_text)
    if match:
        return path_text[: -len(".json")] + "_review.md"
    return str(ROOT / "reports" / "baselane_monthly_accruals_YYYYMM_review.md")


def transfer_review_artifacts(report: dict[str, Any]) -> list[dict[str, Any]]:
    areas: dict[str, set[str]] = {}
    source_blockers = [str(item or "").lower() for item in report.get("source_blockers") or []]
    row_hold_reasons = [
        str(reason or "").lower()
        for row in report.get("rows") or []
        if isinstance(row, dict)
        for reason in (row.get("hold_reasons") or [])
    ]
    if any(item.startswith("required_source_") for item in source_blockers):
        areas.setdefault("required_source_artifacts", set()).update(
            {
                str(report.get("candidate_packet") or ""),
                str(report.get("cf_balance_sheet_report") or ""),
                str(report.get("source_cleanup_queue") or ""),
                str(report.get("source_cash_report") or ""),
                str(report.get("source_cash_reconciliation_actions") or ""),
                str(report.get("monthly_accruals_report") or ""),
            }
        )
    if any("monthly_accruals_" in item for item in source_blockers):
        areas.setdefault("monthly_accruals", set()).update(
            {
                str(report.get("monthly_accruals_report") or ""),
                monthly_accrual_review_markdown_path(report.get("monthly_accruals_report")),
                str(ROOT / "reports" / "baselane_monthly_accrual_gap_approvals_review.csv"),
                str(ROOT / "config" / "baselane_monthly_accrual_gap_approvals.json"),
                str(ROOT / "reports" / "baselane_monthly_accrual_gap_approvals_import.requires-explicit-approval.sh"),
                str(report.get("monthly_accruals_append_audit_report") or ""),
                str(report.get("monthly_accruals_append_audit_decision_report") or ""),
                str(report.get("monthly_accruals_append_audit_restore_commands_file") or ""),
            }
        )
    if any(item.startswith("cf_balance_sheet_") for item in source_blockers) or int(
        report.get("yhome_update_required_count") or 0
    ) > 0:
        areas.setdefault("cf_balance_sheet_yhome", set()).update(
            {
                str(report.get("cf_balance_sheet_report") or ""),
                str(report.get("yhome_update_plan_csv") or ""),
                str(report.get("yhome_missing_candidates_csv") or ""),
                str(ROOT / "reports" / "yhome_operating_cash_apply_verify_report.json"),
                str(ROOT / "reports" / "yhome_operating_cash_gsheet_update_report.json"),
            }
        )
    if any(item.startswith("cf_untagged_") for item in source_blockers):
        areas.setdefault("cf_untagged_review", set()).add(str(report.get("untagged_review_report") or ""))
    if int(report.get("source_cash_reconciliation_action_count") or 0) > 0 or str(
        report.get("source_cash_reconciliation_status") or ""
    ) not in {"", "ok"}:
        areas.setdefault("source_cash_reconciliation", set()).update(
            {
                str(report.get("source_cash_report") or ""),
                str(report.get("source_cash_reconciliation_actions") or ""),
            }
        )
    property_cash_blockers = [
        str(item or "").lower() for item in report.get("property_cash_review_blockers") or []
    ]
    if any("property_cash_review:804 s quitman st" in item for item in property_cash_blockers):
        areas.setdefault("property_cash_review:804 s quitman st", set()).update(
            {
                str(ROOT / "reports" / "baselane_804_quitman_cash_alignment_review.md"),
                str(ROOT / "reports" / "baselane_804_quitman_cash_alignment_group_review_queue.csv"),
                str(ROOT / "config" / "baselane_804_quitman_cash_alignment_reviewed_template.json"),
                str(ROOT / "reports" / "baselane_804_quitman_cash_alignment_import_group_review.requires-explicit-approval.sh"),
            }
        )
    if any("coownership_gl_policy:" in item for item in row_hold_reasons):
        for detail in report.get("coownership_review_details") or []:
            if not isinstance(detail, dict):
                continue
            for key in ("source_report", "protected_review_csv", "protected_review_import_command_file"):
                value = str(detail.get(key) or "")
                if value:
                    areas.setdefault("coownership_85104_preclosing_retag", set()).add(value)
        areas.setdefault("coownership_85104_preclosing_retag", set()).update(
            {
                str(report.get("coownership_validation_report") or ""),
                str(ROOT / "reports" / "baselane_85104_preclosing_property_retag_audit.json"),
                str(ROOT / "reports" / "baselane_85104_preclosing_property_retag_audit.csv"),
                str(ROOT / "reports" / "baselane_85104_preclosing_property_retag_partial_apply.requires-explicit-approval.sh"),
                str(ROOT / "reports" / "baselane_85104_preclosing_protected_row_review_import.requires-explicit-approval.sh"),
            }
        )
    def artifact_sort_key(path: str) -> tuple[int, str]:
        name = Path(path).name
        if name.endswith("_review.md"):
            return (0, path)
        if name == "baselane_85104_preclosing_property_retag_audit.json":
            return (0, path)
        if name.endswith("_review.csv") or name.endswith("_review_queue.csv") or "review_queue" in name:
            return (1, path)
        if name.endswith("_reviewed_template.json") or "gap_approvals.json" in name:
            return (2, path)
        if "requires-explicit-approval" in name:
            return (3, path)
        return (4, path)

    return [
        {
            "review_area": area,
            "artifacts": sorted((path for path in paths if path), key=artifact_sort_key),
            "missing_artifacts": sorted(
                (path for path in paths if path and not Path(path).is_file()),
                key=artifact_sort_key,
            ),
        }
        for area, paths in sorted(areas.items())
    ]


def write_telegram_markdown(path: Path, report: dict[str, Any], *, max_rows: int = 12) -> None:
    rows = list(report.get("rows") or [])
    as_of_months = sorted(
        {
            str(row.get("eco_gl_column_e_as_of_month") or "").strip()
            for row in rows
            if str(row.get("eco_gl_column_e_as_of_month") or "").strip()
        }
    )
    as_of_text = ", ".join(as_of_months) if as_of_months else "unknown"
    send_rows = [row for row in rows if row.get("action") == "send_to_lofty"]
    hold_rows = [row for row in rows if row.get("action") == "hold"]
    held_surplus_rows = [
        row
        for row in hold_rows
        if float(row.get("eco_cash_surplus_above_minimum") or 0.0) > 0
        and float(row.get("eco_cash_shortfall_to_minimum") or 0.0) <= 0
    ]
    top_shortfalls = sorted(
        [
            row
            for row in hold_rows
            if float(row.get("eco_cash_shortfall_to_minimum") or 0.0) > 0
        ],
        key=lambda row: float(row.get("eco_cash_shortfall_to_minimum") or 0.0),
        reverse=True,
    )[:max_rows]
    lines = [
        "Monthly Lofty transfer reconciliation",
        f"Status: {report.get('status')}",
        f"As of: {as_of_text}",
        f"ECO cash basis: {report.get('eco_operating_cash_source_policy') or ECO_OPERATING_CASH_SOURCE_POLICY}",
        f"Reporting month policy: {report.get('eco_operating_cash_reporting_month_policy') or ECO_OPERATING_CASH_REPORTING_MONTH_POLICY}",
        f"Full ECO Operating Cash balance: {compact_money(report.get('eco_operating_cash_full_balance_total'))} (not the Lofty send amount)",
        f"ECO General Ledger balance: {compact_money(report.get('eco_general_ledger_total'))}",
        f"Combined ECO + Lofty OR floor: {compact_money(report.get('eco_minimum'))} per active co-ownership DAO",
        f"Active DAO cash balances: {report.get('active_dao_cash_balance_property_count', 0)}; ECO Net DAO Funds total {compact_money(report.get('active_dao_eco_operating_cash_total'))}; physical bank known total {compact_money(report.get('active_dao_physical_bank_cash_known_total'))}; detail {Path(str(report.get('active_dao_cash_balance_csv') or 'reports/baselane_active_dao_cash_balances.csv')).name}",
        f"Final transfer amounts: {'yes' if report.get('bank_transfer_actions_final') is True else 'no'}",
        f"Approved to send to Lofty now: {compact_money(report.get('approved_send_to_lofty_now_total'))} across {report.get('ready_to_send_property_count', 0)} DAO(s)",
        f"Held surplus, do not send yet: {compact_money(report.get('held_surplus_pending_review_total'))}",
        f"Top up combined reserve before distributions: {compact_money(report.get('combined_reserve_shortfall_total'))}",
        f"Bank action summary: send_to_lofty {compact_money((report.get('bank_action_amount_totals') or {}).get('send_to_lofty'))}; top_up_eco {compact_money((report.get('bank_action_amount_totals') or {}).get('top_up_eco'))}; review/hold {compact_money((report.get('bank_action_amount_totals') or {}).get('review_or_hold'))}",
    ]
    if report.get("recommended_send_to_lofty_total_is_final") is True and report.get("bank_transfer_actions_final") is not True:
        lines.append("Note: ready send_to_lofty rows are final, but one or more DAO bank actions still require hold/review.")
    if report.get("source_clean_for_final_transfer_amounts") is not True:
        lines.append("STOP: source cleanup is not clean; do not move money.")
        for detail in list(report.get("monthly_accruals_amount_mismatch_details") or [])[:3]:
            if not isinstance(detail, dict):
                continue
            lines.append(
                f"Accrual mismatch: {detail.get('property')} {detail.get('month')} {detail.get('kind')} "
                f"current {compact_money(detail.get('current_row_amount'))}, expected {compact_money(detail.get('expected_amount'))}."
            )
        for detail in list(report.get("monthly_accruals_live_plan_update_details") or [])[:3]:
            if not isinstance(detail, dict):
                continue
            lines.append(
                f"Guarded Baselane update needed: id {detail.get('id')} {detail.get('property')} {detail.get('kind')} "
                f"{compact_money(detail.get('absolute_amount'))} on {detail.get('date')}."
            )
    yhome_update_rows = list(report.get("yhome_update_required_rows") or [])
    if yhome_update_rows:
        lines.append(f"Yhome Operating Cash is stale for {len(yhome_update_rows)} row(s); update before final transfers.")
        yhome_details = report.get("yhome_update_required_details")
        if isinstance(yhome_details, list) and yhome_details:
            for detail in yhome_details[:max_rows]:
                if not isinstance(detail, dict):
                    continue
                lines.append(
                    f"  - {detail.get('property')}: row {detail.get('row_number')} {detail.get('column')} "
                    f"{compact_money(detail.get('current_value'))} -> {compact_money(detail.get('target_value'))} "
                    f"(diff {compact_money(detail.get('diff'))})"
                )
    if send_rows:
        lines.append("")
        lines.append("Send to Lofty:")
        for row in send_rows[:max_rows]:
            lines.append(
                f"- {row.get('state')} {row.get('property')}: send {compact_money(row.get('recommended_send_to_lofty_amount'))}; "
                f"ECO {compact_money(row.get('eco_operating_cash'))}; Lofty OR "
                f"{compact_money(row.get('lofty_curr_maintenance_reserve'))}; combined floor "
                f"{compact_money(report.get('eco_minimum'))}"
            )
    if top_shortfalls:
        lines.append("")
        lines.append("Top up combined ECO + Lofty OR reserve first:")
        for row in top_shortfalls:
            lines.append(
                f"- {row.get('state')} {row.get('property')}: ECO {compact_money(row.get('eco_operating_cash'))}, "
                f"Lofty OR {compact_money(row.get('lofty_curr_maintenance_reserve'))}, "
                f"top up {compact_money(row.get('bank_transfer_amount'))}"
            )
    if held_surplus_rows:
        lines.append("")
        lines.append("Surplus held pending guard cleanup:")
        for row in held_surplus_rows[:max_rows]:
            reasons = ", ".join(row.get("hold_reasons") or [])
            suffix = f" ({reasons})" if reasons else ""
            lines.append(
                f"- {row.get('state')} {row.get('property')}: ECO {compact_money(row.get('eco_operating_cash'))}; "
                f"Lofty OR {compact_money(row.get('lofty_curr_maintenance_reserve'))}; "
                f"potential send {compact_money(row.get('eco_cash_surplus_above_minimum'))}; HOLD{suffix}"
            )
    if yhome_update_rows:
        lines.append("")
        lines.append("Yhome updates required:")
        for row in yhome_update_rows[:max_rows]:
            lines.append(
                f"- {row.get('property')}: {row.get('column')} {row.get('current_value')} -> {row.get('target_value_formatted')}"
            )
    lines.append("")
    lines.append(f"Evidence: {DEFAULT_CSV.relative_to(ROOT)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    records = candidate_records(args.candidate_packet)
    candidate_packet_payload = read_json(args.candidate_packet)
    cf_index = cf_summary_index(args.cf_balance_sheet_report)
    inactive_rows = load_inactive_exclusion_rows(args.yhome_csv)
    lofty_manager_properties_response = getattr(args, "lofty_manager_properties_response", None)
    if lofty_manager_properties_response is None:
        lofty_reserve_authority_report = {
            "path": None,
            "status": "not_configured",
            "source_status": "not_configured",
            "live_property_count": 0,
            "candidate_property_count": 0,
            "missing_property_names": [],
            "duplicate_property_names": [],
            "invalid_reserve_property_names": [],
            "source_mode": "legacy_candidate_packet_fallback_for_direct_callers",
        }
        lofty_reserve_authority_blockers = []
        lofty_reserve_overrides = None
    else:
        lofty_reserve_authority_report, lofty_reserve_authority_blockers, lofty_reserve_overrides = lofty_reserve_authority(
            Path(lofty_manager_properties_response),
            records,
            inactive_rows,
        )
    active_cash_balance_rows = active_dao_cash_balance_rows(
        records,
        inactive_rows,
        lofty_reserve_overrides,
    )
    active_cash_balance_blockers = []
    active_cash_balance_missing_source_count = sum(
        1 for row in active_cash_balance_rows if row.get("cash_balance_status") != "ok"
    )
    if active_cash_balance_missing_source_count:
        active_cash_balance_blockers.append(
            f"source_cash_active_dao_missing_canonical_source_count={active_cash_balance_missing_source_count}"
        )
    active_cash_balance_blockers.extend(
        active_dao_cash_balance_integrity_blockers(active_cash_balance_rows)
    )
    yhome_update_required_rows = load_yhome_update_required_rows(args.yhome_update_plan_csv)
    yhome_update_details = yhome_update_required_details(yhome_update_required_rows)
    source_cleanup_queue = read_json(args.source_cleanup_queue)
    source_cash_report = read_json(args.source_cash_report)
    source_cash_report["report_digest"] = sha256_file(args.source_cash_report)
    source_cash_reconciliation_actions = read_json(args.source_cash_reconciliation_actions)
    ecogl_autonomy_report_path = getattr(args, "ecogl_autonomy_report", DEFAULT_ECOGL_AUTONOMY_REPORT)
    ecogl_autonomy_report = read_json(ecogl_autonomy_report_path)
    daily_sync_report_path = getattr(args, "daily_sync_report", None)
    monthly_run_report_path = getattr(args, "monthly_run_report", None)
    daily_sync_report = read_json(daily_sync_report_path) if daily_sync_report_path else {}
    monthly_run_report = read_json(monthly_run_report_path) if monthly_run_report_path else {}
    untagged_review_report_path = getattr(args, "untagged_review_report", None)
    untagged_review_report = read_json(untagged_review_report_path) if untagged_review_report_path else {}
    monthly_accruals_report = read_json(args.monthly_accruals_report)
    monthly_accruals_live_plan = read_json(args.monthly_accruals_live_plan)
    monthly_accruals_live_verify_path = getattr(args, "monthly_accruals_live_verify", None)
    if monthly_accruals_live_verify_path is None:
        monthly_accruals_live_verify_path = args.monthly_accruals_live_plan.with_name(
            args.monthly_accruals_live_plan.name.replace(".live-plan.json", ".live-verify.json")
        )
    monthly_accruals_live_verify = read_json(monthly_accruals_live_verify_path)
    baselane_login_wait_report_path = args.monthly_accruals_live_plan.parent / "baselane_login_wait_report.json"
    baselane_login_wait_report = read_json(baselane_login_wait_report_path)
    monthly_accrual_amount_mismatches = monthly_accrual_amount_mismatch_details(monthly_accruals_report)
    monthly_accrual_live_plan_updates = monthly_accrual_live_plan_update_details(monthly_accruals_live_plan)
    monthly_accruals_append_audit = read_json(args.monthly_accruals_append_audit)
    monthly_accruals_append_audit_decision_path = getattr(
        args,
        "monthly_accruals_append_audit_decision",
        DEFAULT_MONTHLY_ACCRUALS_APPEND_AUDIT_DECISION,
    )
    monthly_accruals_append_audit_decision = read_json(monthly_accruals_append_audit_decision_path)
    monthly_accruals_append_audit_decision["path"] = str(monthly_accruals_append_audit_decision_path)
    monthly_accruals_append_audit_acceptance_report = monthly_accrual_append_audit_acceptance(
        monthly_accruals_append_audit,
        monthly_accruals_append_audit_decision,
    )
    missing_reserve_decision_path = getattr(
        args,
        "missing_reserve_decision_scaffold",
        DEFAULT_MISSING_RESERVE_DECISION_SCAFFOLD,
    )
    missing_reserve_decision_blocker_list, missing_reserve_decision_report = missing_reserve_decision_blockers(
        missing_reserve_decision_path
    )
    cf_balance_sheet_report_payload = read_json(args.cf_balance_sheet_report)
    cf_balance_cross_artifact_mismatch_details = cf_balance_cross_artifact_mismatches(
        active_cash_balance_rows,
        cf_index,
    )
    coownership_policy_blockers = coownership_blockers(args.coownership_validation_report)
    property_cash_review_policy_blockers = property_cash_review_blockers(args.property_cash_review_reports)
    property_cash_details = property_cash_review_details(args.property_cash_review_reports)
    coownership_details = coownership_review_details(args.coownership_validation_report)
    states = {state.strip().upper() for state in args.coownership_states.split(",") if state.strip()}
    candidate_blockers = candidate_packet_blockers(candidate_packet_payload)
    required_source_blockers = required_source_artifact_blockers(
        candidate_packet_payload=candidate_packet_payload,
        candidate_packet_path=args.candidate_packet,
        source_cleanup_queue=source_cleanup_queue,
        source_cleanup_queue_path=args.source_cleanup_queue,
        source_cash_report=source_cash_report,
        source_cash_report_path=args.source_cash_report,
        source_cash_reconciliation_actions=source_cash_reconciliation_actions,
        source_cash_reconciliation_actions_path=args.source_cash_reconciliation_actions,
        ecogl_autonomy_report=ecogl_autonomy_report,
        ecogl_autonomy_report_path=ecogl_autonomy_report_path,
        monthly_accruals_report=monthly_accruals_report,
        monthly_accruals_report_path=args.monthly_accruals_report,
        cf_balance_sheet_report=cf_balance_sheet_report_payload,
        cf_balance_sheet_report_path=args.cf_balance_sheet_report,
        current_run_started_at=getattr(args, "current_run_started_at", None),
    )
    runtime_blockers = operational_runtime_blockers(
        candidate_packet_payload=candidate_packet_payload,
        daily_sync_report=daily_sync_report,
        daily_sync_report_path=daily_sync_report_path,
        monthly_run_report=monthly_run_report,
        monthly_run_report_path=monthly_run_report_path,
        current_run_started_at=getattr(args, "current_run_started_at", None),
    ) if daily_sync_report_path and monthly_run_report_path else []
    raw_accrual_blockers = monthly_accrual_blockers(monthly_accruals_report, inactive_rows)
    accrual_blockers, accrual_live_plan_suppression = suppress_local_monthly_accrual_gap_blockers(
        raw_accrual_blockers,
        monthly_accruals_report,
        monthly_accruals_live_plan,
        monthly_accruals_live_verify,
    )
    accrual_live_plan_blockers = monthly_accrual_live_plan_blockers(
        monthly_accruals_live_plan,
        baselane_login_wait_report,
        monthly_accruals_live_verify,
    )
    accrual_append_audit_blockers = monthly_accrual_append_audit_blockers(
        monthly_accruals_append_audit,
        monthly_accruals_append_audit_decision,
    )
    cf_blockers = cf_report_blockers(cf_balance_sheet_report_payload)
    untagged_blockers = untagged_review_blockers(untagged_review_report)
    if cf_balance_cross_artifact_mismatch_details:
        cf_blockers.append(
            f"cf_balance_cross_artifact_mismatches={len(cf_balance_cross_artifact_mismatch_details)}"
        )
    all_property_cash_review_blocker_reasons = [
        f"property_cash_review:{property_key}:{reason}"
        for property_key, reasons in sorted(property_cash_review_policy_blockers.items())
        for reason in reasons
    ]
    global_blockers = (
        source_blockers(source_cleanup_queue, source_cash_report, source_cash_reconciliation_actions)
        + ecogl_autonomy_blockers(ecogl_autonomy_report)
        + required_source_blockers
        + runtime_blockers
        + candidate_blockers
        + active_cash_balance_blockers
        + accrual_blockers
        + accrual_live_plan_blockers
        + accrual_append_audit_blockers
        + missing_reserve_decision_blocker_list
        + lofty_reserve_authority_blockers
        + cf_blockers
        + untagged_blockers
    )
    blockers = global_blockers
    rows = build_rows(
        records=records,
        cf_index=cf_index,
        inactive_rows=inactive_rows,
        states=states,
        eco_minimum=args.eco_minimum,
        global_source_blockers=global_blockers,
        coownership_policy_blockers=coownership_policy_blockers,
        property_cash_review_policy_blockers=property_cash_review_policy_blockers,
        property_cash_review_details_by_property=property_cash_details,
        monthly_accruals_report=monthly_accruals_report,
        lofty_reserve_overrides=lofty_reserve_overrides,
    )
    monthly_accruals_property_coverage_details = monthly_accrual_coverage_details(monthly_accruals_report)
    monthly_accruals_gap_approval_details = monthly_accrual_gap_approval_details(monthly_accruals_report)
    active_rows = [row for row in rows if row.get("action") != "skip_inactive"]
    active_cash_balance_total_is_complete = not any(
        row.get("cash_balance_status") != "ok" for row in active_cash_balance_rows
    )
    active_cash_balance_known_partial_total = sum_money_values(
        row.get("eco_operating_cash") for row in active_cash_balance_rows
    )
    active_cash_balance_total = (
        active_cash_balance_known_partial_total if active_cash_balance_total_is_complete else None
    )
    ledger_evidence = canonical_ledger_evidence(active_cash_balance_rows)
    property_cash_review_blocker_reasons = scoped_property_cash_review_blockers(
        property_cash_review_policy_blockers,
        active_rows,
    )
    active_property_cash_details = scoped_property_cash_review_details(property_cash_details, active_rows)
    inactive_property_cash_details = [
        detail
        for detail in property_cash_details
        if detail not in active_property_cash_details
    ]
    action_counts = Counter(row["action"] for row in rows)
    bank_action_counts = Counter(str(row.get("bank_transfer_action") or "") for row in rows if row.get("bank_transfer_action"))
    bank_action_amount_totals = Counter()
    for row in active_rows:
        action_name = str(row.get("bank_transfer_action") or "").strip()
        if not action_name:
            continue
        amount = parse_money(row.get("bank_transfer_amount"))
        if amount is not None:
            bank_action_amount_totals[action_name] += amount
    review_or_hold_total = sum(
        float(row.get("provisional_send_to_lofty_amount") or 0.0)
        for row in rows
        if row.get("action") == "hold"
        and row.get("bank_transfer_action") in {"hold", "review_before_transfer", "fix_source_tagging_before_transfer"}
    )
    bank_action_amount_totals["review_or_hold"] = round(review_or_hold_total, 2)
    for action_name in ("send_to_lofty", "top_up_eco", "hold", "review_before_transfer", "fix_source_tagging_before_transfer"):
        bank_action_amount_totals.setdefault(action_name, 0.0)
    missing_bank_action_count = sum(1 for row in rows if not str(row.get("bank_transfer_action") or "").strip())
    ready_recommended_values = [
        row.get("recommended_send_to_lofty_amount")
        for row in active_rows
        if row.get("action") == "send_to_lofty"
    ]
    recommended_total = round(sum(float(value or 0.0) for value in ready_recommended_values), 2)
    coownership_eco_operating_cash_full_balance_total = round(
        sum(float(row.get("eco_operating_cash") or 0.0) for row in active_rows),
        2,
    )
    eco_operating_cash_full_balance_total = active_cash_balance_total
    eco_general_ledger_total = sum_money_values(
        row.get("eco_gl_column_e_sum") for row in active_cash_balance_rows
    )
    physical_bank_cash_known_total = sum_money_values(
        row.get("physical_bank_cash") for row in active_cash_balance_rows
    )
    physical_bank_cash_mapped_count = sum(
        1 for row in active_cash_balance_rows if row.get("physical_bank_cash") is not None
    )
    physical_bank_cash_missing_count = len(active_cash_balance_rows) - physical_bank_cash_mapped_count
    eco_operating_cash_full_balance_recomputed_total = (
        float(round(
            sum(
                Decimal(str(row.get("eco_operating_cash")))
                for row in active_cash_balance_rows
                if row.get("eco_operating_cash") is not None
            ),
            2,
        ))
        if active_cash_balance_total_is_complete
        else None
    )
    eco_operating_cash_full_balance_reconciliation_difference = (
        float(round(
            Decimal(str(eco_operating_cash_full_balance_total))
            - Decimal(str(eco_operating_cash_full_balance_recomputed_total)),
            2,
        ))
        if eco_operating_cash_full_balance_total is not None
        and eco_operating_cash_full_balance_recomputed_total is not None
        else None
    )
    lofty_curr_maintenance_reserve_total = round(
        sum(float(row.get("lofty_curr_maintenance_reserve") or 0.0) for row in active_rows),
        2,
    )
    total_operating_cash_for_distribution_test_total = round(
        sum(float(row.get("total_operating_cash_for_distribution_test") or 0.0) for row in active_rows),
        2,
    )
    provisional_total = round(sum(float(row.get("provisional_send_to_lofty_amount") or 0.0) for row in active_rows), 2)
    held_surplus_pending_review_total = round(
        sum(
            float(row.get("provisional_send_to_lofty_amount") or 0.0)
            for row in active_rows
            if row.get("action") == "hold"
        ),
        2,
    )
    shortfall_total = round(
        sum(
            float(row.get("combined_reserve_shortfall_to_floor") or 0.0)
            for row in active_rows
        ),
        2,
    )
    missing_lofty_reserve_rows = [
        {
            "property": row.get("property"),
            "state": row.get("state"),
            "property_path": row.get("property_path"),
            "eco_operating_cash": row.get("eco_operating_cash"),
            "eco_gl_column_e_source": row.get("eco_gl_column_e_source"),
            "next_action": "Refresh the live Lofty manager property response and regenerate the monthly candidate packet and transfer reconciliation.",
        }
        for row in rows
        if row.get("lofty_curr_maintenance_reserve_missing_treated_as_zero") is True
    ]
    hold_count = sum(1 for row in rows if row.get("action") == "hold")
    unresolved_hold_count = sum(
        1
        for row in rows
        if row.get("action") == "hold"
        and row.get("bank_transfer_action") != "top_up_eco"
    )
    source_clean = not blockers
    bank_transfer_actions_final = source_clean and unresolved_hold_count == 0 and missing_bank_action_count == 0
    empty_candidate_blocked = bool(not records and (
        coownership_policy_blockers
        or property_cash_review_policy_blockers
        or blockers
    ))
    status = "ok"
    if empty_candidate_blocked:
        status = "blocked_empty_candidate_packet"
        recommended_total = None
    elif not source_clean:
        status = "blocked_source_not_clean"
        recommended_total = None
    elif unresolved_hold_count:
        status = "review"
    report = {
        "job": "baselane-lofty-transfer-requirements",
        "generated_at": iso_z(),
        "status": status,
        "month": getattr(args, "month", None),
        "run_month": getattr(args, "month", None),
        "reporting_month": getattr(args, "month", None),
        "reporting_cutoff_date": getattr(args, "reporting_cutoff_date", None),
        "coownership_states": sorted(states),
        "eco_minimum": round(float(args.eco_minimum), 2),
        "eco_operating_cash_source_policy": ECO_OPERATING_CASH_SOURCE_POLICY,
        "source_cash_balance_policy": ECO_OPERATING_CASH_SOURCE_POLICY,
        "eco_operating_cash_reporting_month_policy": ECO_OPERATING_CASH_REPORTING_MONTH_POLICY,
        "eco_operating_cash_full_balance_total": eco_operating_cash_full_balance_total,
        "eco_operating_cash_full_balance_recomputed_total": eco_operating_cash_full_balance_recomputed_total,
        "eco_operating_cash_full_balance_reconciliation_difference": eco_operating_cash_full_balance_reconciliation_difference,
        "eco_operating_cash_full_balance_reconciliation_status": (
            "ok"
            if eco_operating_cash_full_balance_reconciliation_difference == 0
            else "review"
        ),
        "eco_general_ledger_total": eco_general_ledger_total,
        "coownership_eco_operating_cash_full_balance_total": coownership_eco_operating_cash_full_balance_total,
        "eco_operating_cash_full_balance_total_policy": (
            "Portfolio ECO Net DAO Funds is the sum of each active DAO's dated verified ECO-held "
            "unrestricted cash after recorded obligations and restrictions. The full property ledger "
            "and physical bank cash are separate reconciliation controls."
        ),
        "lofty_curr_maintenance_reserve_total": lofty_curr_maintenance_reserve_total,
        "active_dao_cash_balance_property_count": len(active_cash_balance_rows),
        "active_dao_property_count": len(active_cash_balance_rows),
        "transfer_candidate_property_count": len(rows),
        "transfer_candidate_scope": (
            "coownership_states_only; full active-DAO ECO balances are in active_dao_cash_balance_rows"
        ),
        "active_dao_cash_balance_missing_source_count": sum(
            1 for row in active_cash_balance_rows if row.get("cash_balance_status") != "ok"
        ),
        "active_dao_eco_operating_cash_total": active_cash_balance_total,
        "active_dao_eco_operating_cash_known_partial_total": active_cash_balance_known_partial_total,
        "active_dao_eco_operating_cash_total_is_complete": active_cash_balance_total_is_complete,
        "active_dao_physical_bank_cash_known_total": physical_bank_cash_known_total,
        "active_dao_physical_bank_cash_mapped_count": physical_bank_cash_mapped_count,
        "active_dao_physical_bank_cash_missing_count": physical_bank_cash_missing_count,
        "active_dao_cash_balance_blockers": active_cash_balance_blockers,
        "canonical_ledger_evidence": ledger_evidence,
        "canonical_ledger_fingerprint_sha256": ledger_evidence["fingerprint_sha256"],
        "canonical_ledger_evidence_status": ledger_evidence["status"],
        "active_dao_lofty_curr_maintenance_reserve_total": sum_money_values(
            row.get("lofty_curr_maintenance_reserve") for row in active_cash_balance_rows
        ),
        "active_dao_combined_eco_and_lofty_reserve_total": sum_money_values(
            row.get("combined_eco_and_lofty_reserve") for row in active_cash_balance_rows
        ),
        "active_dao_cash_balance_policy": (
            "Every active candidate DAO must have a verified dated unrestricted-cash balance and a "
            "canonical full-property GL control. A mapped Baselane bank balance and bank-minus-GL gap "
            "are separate reconciliation evidence."
        ),
        "active_dao_cash_balance_csv": str(getattr(args, "cash_balance_csv", DEFAULT_CASH_BALANCE_CSV)),
        "total_operating_cash_for_distribution_test_total": total_operating_cash_for_distribution_test_total,
        "transfer_formula": (
            "min(max(0, eco_operating_cash), "
            "max(0, eco_operating_cash + lofty_operating_reserve - combined_reserve_floor))"
        ),
        "transfer_policy": (
            "The $3,000 co-ownership reserve floor is measured across ECO-held spendable cash plus "
            "positive Lofty Operating Reserve. Sendable cash is the combined surplus, capped by "
            "non-negative cash actually held by ECO; "
            "no-DAO-mortgage responsibility properties do not report provisional send amounts while source cleanup is unresolved."
        ),
        "no_dao_mortgage_responsibility_policy_properties": sorted(NO_DAO_MORTGAGE_PROPERTY_KEYS),
        "source_clean_for_final_transfer_amounts": source_clean,
        "recommended_send_to_lofty_total_is_final": source_clean,
        "bank_transfer_actions_final": bank_transfer_actions_final,
        "bank_transfer_actions_final_policy": (
            "True only when source data is clean, every active DAO has a concrete bank_transfer_action, "
            "and no active row remains in unresolved hold/review. This is stricter than "
            "recommended_send_to_lofty_total_is_final, which certifies only the ready send_to_lofty rows."
        ),
        "recommended_send_to_lofty_total_policy": (
            "Total sums ready send_to_lofty rows only, using combined ECO-held spendable cash plus Lofty OR "
            "less the reserve floor, capped by ECO-held cash. Held rows remain excluded and surface "
            "top-up/review reasons separately."
        ),
        "recommended_send_to_lofty_total_is_cash_balance": False,
        "eco_operating_cash_vs_send_to_lofty_policy": (
            "Do not use recommended_send_to_lofty_total as the DAO cash balance. "
            "Use eco_operating_cash_full_balance_total for spendable ECO Net DAO Funds; "
            "use active_dao_physical_bank_cash_known_total only as non-authoritative custody evidence; "
            "use coownership_eco_operating_cash_full_balance_total only for the co-ownership transfer subset; "
            "use recommended_send_to_lofty_total only for approved surplus transfer/distribution after all gates are clean."
        ),
        "source_blockers": blockers,
        "source_blocker_count": len(blockers),
        "source_blocker_summary": source_blocker_summary(blockers),
        "ecogl_autonomy_report": str(ecogl_autonomy_report_path),
        "ecogl_autonomy_status": ecogl_autonomy_report.get("status"),
        "ecogl_autonomy_downstream_hold": ecogl_autonomy_report.get("downstream_hold") is True,
        "ecogl_autonomy_exception_count": int(ecogl_autonomy_report.get("exception_count") or 0),
        "required_source_blockers": required_source_blockers,
        "required_source_blocker_count": len(required_source_blockers),
        "operational_runtime_blockers": runtime_blockers,
        "operational_runtime_blocker_count": len(runtime_blockers),
        "daily_sync_report": str(daily_sync_report_path) if daily_sync_report_path else None,
        "daily_sync_report_status": daily_sync_report.get("status"),
        "daily_sync_report_generated_at": daily_sync_report.get("generated_at"),
        "monthly_run_report": str(monthly_run_report_path) if monthly_run_report_path else None,
        "monthly_run_report_status": monthly_run_report.get("status") or monthly_run_report.get("effective_status"),
        "monthly_run_report_generated_at": monthly_run_report.get("generated_at") or monthly_run_report.get("ended_at"),
        "candidate_packet_blockers": candidate_blockers,
        "monthly_accruals_report": str(args.monthly_accruals_report),
        "monthly_accruals_live_plan_report": str(args.monthly_accruals_live_plan),
        "monthly_accruals_live_verify_report": str(monthly_accruals_live_verify_path),
        "monthly_accruals_live_plan_status": monthly_accruals_live_plan.get("status"),
        "monthly_accruals_live_verify_status": monthly_accruals_live_verify.get("status"),
        "monthly_accruals_live_plan_auth_blocked": monthly_accruals_live_plan.get("auth_blocked") is True,
        "monthly_accruals_live_plan_cdp_blocked": monthly_accruals_live_plan.get("cdp_blocked") is True,
        "baselane_login_wait_report": str(baselane_login_wait_report_path),
        "baselane_login_wait_status": baselane_login_wait_report.get("status"),
        "baselane_login_wait_reason": baselane_login_wait_report.get("reason"),
        "baselane_login_wait_recaptcha_present": baselane_login_wait_report.get("recaptcha_present") is True,
        "baselane_login_wait_next_action": baselane_login_wait_report.get("next_action"),
        "monthly_accruals_live_plan_target_count": int(monthly_accruals_live_plan.get("target_count") or 0),
        "monthly_accruals_live_plan_create_count": int(monthly_accruals_live_plan.get("create_count") or 0),
        "monthly_accruals_live_plan_update_count": int(monthly_accruals_live_plan.get("update_count") or 0),
        "monthly_accruals_live_plan_update_details": monthly_accrual_live_plan_updates,
        "monthly_accruals_live_plan_update_detail_count": len(monthly_accrual_live_plan_updates),
        "monthly_accruals_live_plan_skip_count": int(monthly_accruals_live_plan.get("skip_count") or 0),
        "monthly_accruals_live_plan_issue_count": int(monthly_accruals_live_plan.get("issue_count") or 0),
        "monthly_accruals_live_plan_target_digest": monthly_accruals_live_plan.get("target_digest"),
        "monthly_accruals_live_verify_target_digest": monthly_accruals_live_verify.get("target_digest"),
        "monthly_accruals_live_plan_blockers": accrual_live_plan_blockers,
        "monthly_accruals_live_plan_suppression": accrual_live_plan_suppression,
        "monthly_accruals_append_audit_report": str(args.monthly_accruals_append_audit),
        "monthly_accruals_append_audit_decision_report": str(monthly_accruals_append_audit_decision_path),
        "monthly_accruals_append_audit_acceptance": monthly_accruals_append_audit_acceptance_report,
        "missing_lofty_reserve_decision_scaffold": str(missing_reserve_decision_path),
        "missing_lofty_reserve_decision_validation": missing_reserve_decision_report,
        "missing_lofty_reserve_decision_blockers": missing_reserve_decision_blocker_list,
        "lofty_manager_properties_response": (
            str(lofty_manager_properties_response) if lofty_manager_properties_response is not None else None
        ),
        "lofty_reserve_authority": lofty_reserve_authority_report,
        "lofty_reserve_authority_blockers": lofty_reserve_authority_blockers,
        "monthly_accruals_append_audit_status": monthly_accruals_append_audit.get("status"),
        "monthly_accruals_append_audit_safe_to_restore_baseline": monthly_accruals_append_audit.get("safe_to_restore_baseline"),
        "monthly_accruals_append_audit_added_aops_count": int(monthly_accruals_append_audit.get("added_aops_count") or 0),
        "monthly_accruals_append_audit_added_non_aops_count": int(monthly_accruals_append_audit.get("added_non_aops_count") or 0),
        "monthly_accruals_append_audit_removed_count": int(monthly_accruals_append_audit.get("removed_count") or 0),
        "monthly_accruals_append_audit_added_aops_amount_sum": parse_money(monthly_accruals_append_audit.get("added_aops_amount_sum")),
        "monthly_accruals_append_audit_restore_command": (
            monthly_accruals_append_audit.get("restore_command_requires_explicit_operator_execution")
            or monthly_accruals_append_audit.get("restore_command")
        ),
        "monthly_accruals_append_audit_restore_command_safe_to_write": (
            monthly_accruals_append_audit.get("restore_command_safe_to_write") is True
        ),
        "monthly_accruals_append_audit_restore_command_safety_blockers": (
            monthly_accruals_append_audit.get("restore_command_safety_blockers")
            if isinstance(monthly_accruals_append_audit.get("restore_command_safety_blockers"), list)
            else []
        ),
        "monthly_accruals_append_audit_restore_commands_file": (
            monthly_accruals_append_audit.get("restore_commands_requires_explicit_operator_execution", {}).get("path")
            if isinstance(monthly_accruals_append_audit.get("restore_commands_requires_explicit_operator_execution"), dict)
            else monthly_accruals_append_audit.get("restore_commands_requires_explicit_operator_execution_file")
        ),
        "monthly_accruals_status": monthly_accruals_report.get("status"),
        "monthly_accruals_missing_count": int(monthly_accruals_report.get("missing_count") or 0),
        "monthly_accruals_amount_mismatch_count": int(monthly_accruals_report.get("amount_mismatch_count") or 0),
        "monthly_accruals_amount_mismatch_details": monthly_accrual_amount_mismatches,
        "monthly_accruals_amount_mismatch_detail_count": len(monthly_accrual_amount_mismatches),
        "monthly_accruals_blocked_first_day_pm_fee_count": int(monthly_accruals_report.get("blocked_first_day_pm_fee_count") or 0),
        "monthly_accruals_blocking_gap_action_count": int(monthly_accruals_report.get("blocking_gap_action_count") or 0),
        "monthly_accruals_gap_approval_status": (
            monthly_accruals_report.get("gap_approvals", {}).get("status")
            if isinstance(monthly_accruals_report.get("gap_approvals"), dict)
            else None
        ),
        "monthly_accruals_gap_approval_approved_count": (
            int(monthly_accruals_report.get("gap_approvals", {}).get("approved_count") or 0)
            if isinstance(monthly_accruals_report.get("gap_approvals"), dict)
            else 0
        ),
        "monthly_accruals_gap_approval_issue_count": (
            int(monthly_accruals_report.get("gap_approvals", {}).get("issue_count") or 0)
            if isinstance(monthly_accruals_report.get("gap_approvals"), dict)
            else 0
        ),
        "monthly_accruals_active_without_template_count": int(
            monthly_accruals_report.get("active_without_accrual_template_count")
            or monthly_accruals_report.get("active_without_template_count")
            or 0
        ),
        "monthly_accruals_active_without_fixed_requirement_count": int(
            monthly_accruals_report.get("active_without_fixed_accrual_requirement_count") or 0
        ),
        "monthly_accruals_expected_fixed_coverage_count": int(monthly_accruals_report.get("expected_fixed_accrual_coverage_count") or 0),
        "monthly_accruals_covered_fixed_coverage_count": int(monthly_accruals_report.get("covered_fixed_accrual_coverage_count") or 0),
        "monthly_accruals_missing_fixed_coverage_count": int(monthly_accruals_report.get("missing_fixed_accrual_coverage_count") or 0),
        "monthly_accruals_property_coverage_details": monthly_accruals_property_coverage_details,
        "monthly_accruals_property_coverage_detail_count": len(monthly_accruals_property_coverage_details),
        "monthly_accruals_gap_approval_details": monthly_accruals_gap_approval_details,
        "monthly_accruals_gap_approval_detail_count": len(monthly_accruals_gap_approval_details),
        "monthly_accruals_expected_fixed_coverage_by_kind": monthly_accruals_report.get("expected_fixed_accrual_coverage_by_kind") if isinstance(monthly_accruals_report.get("expected_fixed_accrual_coverage_by_kind"), dict) else {},
        "monthly_accruals_covered_fixed_coverage_by_kind": monthly_accruals_report.get("covered_fixed_accrual_coverage_by_kind") if isinstance(monthly_accruals_report.get("covered_fixed_accrual_coverage_by_kind"), dict) else {},
        "monthly_accruals_missing_fixed_coverage_by_kind": monthly_accruals_report.get("missing_fixed_accrual_coverage_by_kind") if isinstance(monthly_accruals_report.get("missing_fixed_accrual_coverage_by_kind"), dict) else {},
        "monthly_accruals_missing_fixed_coverage": (
            monthly_accruals_report.get("missing_fixed_accrual_coverage")
            if isinstance(monthly_accruals_report.get("missing_fixed_accrual_coverage"), list)
            else []
        ),
        "monthly_accruals_pm_fee_basis_gap_count": int(monthly_accruals_report.get("pm_fee_basis_gap_count") or 0),
        "monthly_accruals_unapproved_pm_fee_basis_gap_count": int(
            monthly_accruals_report.get("unapproved_pm_fee_basis_gap_count")
            if monthly_accruals_report.get("unapproved_pm_fee_basis_gap_count") is not None
            else monthly_accruals_report.get("pm_fee_basis_gap_count")
            or 0
        ),
        "monthly_accruals_pm_fee_basis_gaps": (
            monthly_accruals_report.get("pm_fee_basis_gaps")
            if isinstance(monthly_accruals_report.get("pm_fee_basis_gaps"), list)
            else []
        ),
        "monthly_accruals_audit_policy": (
            "missing_count, amount_mismatch_count, blocked_first_day_pm_fee_count, and "
            "active_without_accrual_template_count block final transfer amounts; "
            "active_without_fixed_accrual_requirement_count is audit-only for direct-ledger properties."
        ),
        "monthly_accruals_blockers": accrual_blockers,
        "monthly_accruals_raw_blockers": raw_accrual_blockers,
        "monthly_accruals_append_audit_blockers": accrual_append_audit_blockers,
        "cf_balance_sheet_blockers": cf_blockers,
        "cf_balance_cross_artifact_mismatch_count": len(cf_balance_cross_artifact_mismatch_details),
        "cf_balance_cross_artifact_mismatches": cf_balance_cross_artifact_mismatch_details[:100],
        "coownership_validation_report": str(args.coownership_validation_report),
        "coownership_review_details": coownership_details,
        "coownership_review_detail_count": len(coownership_details),
        "candidate_packet": str(args.candidate_packet),
        "candidate_packet_status": candidate_packet_payload.get("status"),
        "candidate_packet_record_count": len(records),
        "empty_candidate_packet_blocked": empty_candidate_blocked,
        "empty_candidate_packet_blocker_policy": (
            "A zero-record review candidate packet cannot produce a final transfer reconciliation "
            "while upstream policy, source, or property-cash blockers exist. "
            "The Yhome spreadsheet work product is informational and nonblocking."
        ),
        "cf_balance_sheet_report": str(args.cf_balance_sheet_report),
        "untagged_review_report": str(untagged_review_report_path),
        "untagged_review_status": untagged_review_report.get("status"),
        "untagged_row_count": int(untagged_review_report.get("effective_untagged_row_count", untagged_review_report.get("untagged_row_count")) or 0),
        "untagged_review_required_count": int(untagged_review_report.get("effective_review_required_count", untagged_review_report.get("review_required_count")) or 0),
        "untagged_review_raw_row_count": int(untagged_review_report.get("untagged_row_count") or 0),
        "untagged_review_raw_required_count": int(untagged_review_report.get("review_required_count") or 0),
        "untagged_review_blockers": untagged_blockers,
        "source_cleanup_queue": str(args.source_cleanup_queue),
        "source_cash_report": str(args.source_cash_report),
        "source_cash_report_generated_at": source_cash_report.get("generated_at"),
        "source_cash_report_digest": source_cash_report.get("report_digest"),
        "source_cash_reconciliation_actions": str(args.source_cash_reconciliation_actions),
        "source_cash_reconciliation_status": source_cash_reconciliation_actions.get("status"),
        "source_cash_reconciliation_actions_generated_at": source_cash_reconciliation_actions.get("generated_at"),
        "source_cash_reconciliation_actions_stale": bool(
            parse_iso_z(source_cash_report.get("generated_at"))
            and parse_iso_z(source_cash_reconciliation_actions.get("generated_at"))
            and parse_iso_z(source_cash_reconciliation_actions.get("generated_at")) < parse_iso_z(source_cash_report.get("generated_at"))
        ),
        "source_cash_reconciliation_actions_source_cash_report_digest": source_cash_reconciliation_actions.get("source_cash_report_digest"),
        "source_cash_reconciliation_actions_source_cash_report_digest_matches_current": (
            bool(source_cash_report.get("report_digest"))
            and bool(source_cash_reconciliation_actions.get("source_cash_report_digest"))
            and source_cash_report.get("report_digest") == source_cash_reconciliation_actions.get("source_cash_report_digest")
        ),
        "source_cash_reconciliation_action_count": int(source_cash_reconciliation_actions.get("action_count") or 0),
        "source_cash_reconciliation_active_monthly_candidate_action_count": int(
            source_cash_reconciliation_actions.get("active_monthly_candidate_action_count") or 0
        ),
        "source_cash_reconciliation_action_kind_counts": source_cash_reconciliation_actions.get("action_kind_counts") or {},
        "source_cash_reconciliation_action_scope_counts": source_cash_reconciliation_actions.get("action_scope_counts") or {},
        "source_cash_reconciliation_active_monthly_candidate_actions": (
            source_cash_reconciliation_actions.get("active_monthly_candidate_actions_bounded")
            if isinstance(source_cash_reconciliation_actions.get("active_monthly_candidate_actions_bounded"), list)
            else []
        )[:10],
        "source_cash_reconciliation_active_monthly_candidate_source_cash_mismatch_count": int(
            source_cash_reconciliation_actions.get("active_monthly_candidate_source_cash_mismatch_count") or 0
        ),
        "source_cash_reconciliation_active_monthly_candidate_source_cash_abs_delta_total": parse_money(
            source_cash_reconciliation_actions.get("active_monthly_candidate_source_cash_abs_delta_total")
        ),
        "coownership_validation_report": str(args.coownership_validation_report),
        "coownership_validation_blocked_property_count": len(coownership_policy_blockers),
        "property_cash_review_reports": [str(path) for path in args.property_cash_review_reports],
        "property_cash_review_scope_policy": (
            "property_cash_review_blockers/details are active transfer-candidate scoped. "
            "all_property_cash_review_blockers/details retain non-active or operationally ignored review artifacts without blocking active transfers."
        ),
        "property_cash_review_blocked_property_count": len(property_cash_review_blocker_reasons),
        "property_cash_review_blockers": property_cash_review_blocker_reasons,
        "property_cash_review_details": active_property_cash_details,
        "all_property_cash_review_blocked_property_count": len(property_cash_review_policy_blockers),
        "all_property_cash_review_blockers": all_property_cash_review_blocker_reasons,
        "all_property_cash_review_details": property_cash_details,
        "inactive_or_out_of_scope_property_cash_review_details": inactive_property_cash_details,
        "yhome_csv": str(args.yhome_csv),
        "yhome_update_plan_csv": str(args.yhome_update_plan_csv),
        "yhome_missing_candidates_csv": str(cf_balance_sheet_report_payload.get("yhome_missing_candidates_csv") or ""),
        "yhome_update_required_count": len(yhome_update_required_rows),
        "yhome_update_required_rows": yhome_update_required_rows[:100],
        "yhome_update_required_details": yhome_update_details[:100],
        "yhome_required_states": cf_balance_sheet_report_payload.get("yhome_required_states") or [],
        "yhome_excluded_candidate_count": int(cf_balance_sheet_report_payload.get("yhome_excluded_candidate_count") or 0),
        "yhome_excluded_candidates": (
            cf_balance_sheet_report_payload.get("yhome_excluded_candidates")
            if isinstance(cf_balance_sheet_report_payload.get("yhome_excluded_candidates"), list)
            else []
        )[:100],
        "property_count": len(rows),
        "missing_lofty_reserve_count": len(missing_lofty_reserve_rows),
        "missing_lofty_reserve_rows": missing_lofty_reserve_rows,
        "action_counts": dict(sorted(action_counts.items())),
        "bank_action_counts": dict(sorted(bank_action_counts.items())),
        "bank_action_amount_totals": {
            key: round(float(value), 2) for key, value in sorted(bank_action_amount_totals.items())
        },
        "missing_bank_action_count": missing_bank_action_count,
        "ready_to_send_property_count": action_counts.get("send_to_lofty", 0),
        "held_property_count": hold_count,
        "unresolved_hold_property_count": unresolved_hold_count,
        "recommended_send_to_lofty_total": recommended_total,
        "approved_send_to_lofty_now_total": recommended_total if recommended_total is not None else 0.0,
        "held_surplus_pending_review_total": held_surplus_pending_review_total,
        "provisional_send_to_lofty_total": provisional_total,
        "combined_reserve_shortfall_total": shortfall_total,
        "eco_cash_shortfall_total": shortfall_total,
        "telegram_summary": str(args.telegram_markdown),
        "rows": rows,
        "active_dao_cash_balance_rows": active_cash_balance_rows,
    }
    review_artifacts = transfer_review_artifacts(report)
    report["review_artifacts"] = review_artifacts
    report["review_artifact_area_count"] = len(review_artifacts)
    report["review_missing_artifact_count"] = sum(len(item.get("missing_artifacts") or []) for item in review_artifacts)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Compute guarded Lofty transfer amounts from ECO operating cash.")
    parser.add_argument("--month", help="Target month in YYYY-MM; selects reports/baselane_monthly_accruals_YYYYMM.json unless --monthly-accruals-report is provided.")
    parser.add_argument(
        "--reporting-cutoff-date",
        help="Close cutoff in YYYY-MM-DD; source artifacts are expected to use the same cutoff.",
    )
    parser.add_argument("--candidate-packet", type=Path, default=DEFAULT_CANDIDATE_PACKET)
    parser.add_argument("--cf-balance-sheet-report", type=Path, default=DEFAULT_CF_BALANCE_REPORT)
    parser.add_argument("--source-cleanup-queue", type=Path, default=DEFAULT_SOURCE_CLEANUP_QUEUE)
    parser.add_argument("--source-cash-report", type=Path, default=DEFAULT_SOURCE_CASH_REPORT)
    parser.add_argument("--source-cash-reconciliation-actions", type=Path, default=DEFAULT_SOURCE_CASH_RECONCILIATION_ACTIONS)
    parser.add_argument("--ecogl-autonomy-report", type=Path, default=DEFAULT_ECOGL_AUTONOMY_REPORT)
    parser.add_argument("--daily-sync-report", type=Path, default=DEFAULT_DAILY_SYNC_REPORT)
    parser.add_argument("--monthly-run-report", type=Path, default=DEFAULT_MONTHLY_RUN_REPORT)
    parser.add_argument(
        "--current-run-started-at",
        help="UTC start timestamp for an active monthly run; prevents reuse of older run-scoped artifacts.",
    )
    parser.add_argument("--untagged-review-report", type=Path, default=DEFAULT_UNTAGGED_REVIEW_REPORT)
    parser.add_argument("--monthly-accruals-report", type=Path, default=None)
    parser.add_argument("--monthly-accruals-live-plan", type=Path, default=None)
    parser.add_argument("--monthly-accruals-live-verify", type=Path, default=None)
    parser.add_argument("--monthly-accruals-append-audit", type=Path, default=DEFAULT_MONTHLY_ACCRUALS_APPEND_AUDIT)
    parser.add_argument("--monthly-accruals-append-audit-decision", type=Path, default=DEFAULT_MONTHLY_ACCRUALS_APPEND_AUDIT_DECISION)
    parser.add_argument(
        "--lofty-manager-properties-response",
        type=Path,
        default=DEFAULT_LOFTY_MANAGER_PROPERTIES_RESPONSE,
        help="Current live Lofty manager property response used as the sole reserve authority.",
    )
    parser.add_argument("--missing-reserve-decision-scaffold", type=Path, default=DEFAULT_MISSING_RESERVE_DECISION_SCAFFOLD)
    parser.add_argument("--coownership-validation-report", type=Path, default=DEFAULT_COOWNERSHIP_VALIDATION_REPORT)
    parser.add_argument(
        "--property-cash-review-report",
        dest="property_cash_review_reports",
        type=Path,
        action="append",
        default=DEFAULT_PROPERTY_CASH_REVIEW_REPORTS.copy(),
    )
    parser.add_argument("--yhome-csv", type=Path, default=DEFAULT_YHOME_CSV)
    parser.add_argument("--yhome-update-plan-csv", type=Path, default=DEFAULT_YHOME_UPDATE_PLAN)
    parser.add_argument("--coownership-states", default=",".join(DEFAULT_COWNERSHIP_STATES))
    parser.add_argument("--eco-minimum", type=float, default=DEFAULT_ECO_MINIMUM)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--monthly-report-alias",
        type=Path,
        default=DEFAULT_MONTHLY_RECONCILIATION_REPORT,
        help="Compatibility JSON path for monthly transfer reconciliation consumers.",
    )
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--cash-balance-csv", type=Path, default=DEFAULT_CASH_BALANCE_CSV)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MD)
    parser.add_argument("--telegram-markdown", type=Path, default=DEFAULT_TELEGRAM_MD)
    args = parser.parse_args()
    if args.monthly_accruals_report is None:
        args.monthly_accruals_report = monthly_accrual_report_for_month(args.month)
    if args.monthly_accruals_live_plan is None:
        args.monthly_accruals_live_plan = monthly_accrual_live_plan_for_month(args.month)

    report = build_report(args)
    report["canonical_report"] = str(args.report)
    report["monthly_transfer_reconciliation_report"] = str(args.monthly_report_alias)
    report["legacy_monthly_transfer_reconciliation_report"] = str(DEFAULT_LEGACY_MONTHLY_RECONCILIATION_REPORT)
    write_json_report_with_alias(
        report,
        args.report,
        args.monthly_report_alias,
        DEFAULT_LEGACY_MONTHLY_RECONCILIATION_REPORT,
    )
    write_csv_report(args.csv, report["rows"])
    write_cash_balance_csv(args.cash_balance_csv, report["active_dao_cash_balance_rows"])
    write_markdown(args.markdown, report)
    write_telegram_markdown(args.telegram_markdown, report)
    report["telegram_summary_sha256"] = hashlib.sha256(
        args.telegram_markdown.read_bytes()
    ).hexdigest()
    write_json_report_with_alias(
        report,
        args.report,
        args.monthly_report_alias,
        DEFAULT_LEGACY_MONTHLY_RECONCILIATION_REPORT,
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "property_count": report["property_count"],
                "active_dao_property_count": report["active_dao_property_count"],
                "transfer_candidate_property_count": report["transfer_candidate_property_count"],
                "ready_to_send_property_count": report["ready_to_send_property_count"],
                "held_property_count": report["held_property_count"],
                "recommended_send_to_lofty_total": report["recommended_send_to_lofty_total"],
                "provisional_send_to_lofty_total": report["provisional_send_to_lofty_total"],
                "report": str(args.report),
                "csv": str(args.csv),
                "telegram_markdown": str(args.telegram_markdown),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
