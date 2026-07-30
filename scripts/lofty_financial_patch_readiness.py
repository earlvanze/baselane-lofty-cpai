#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from coownership_reserve_policy import (
    FULL_REPLENISHMENT_THRESHOLD,
    MAINTENANCE_RESERVE_TARGET,
    canonical_property as canonical_reserve_property,
    replenishment_rate,
)
from lofty_financial_approval_manifest import approved_candidate, load as load_financial_approval_manifest

try:
    from lofty_monthly_exclusions import DEFAULT_MANUAL_EXCLUDED_PROPERTIES
except ImportError:  # pragma: no cover - keeps the standalone script usable if copied without helpers.
    DEFAULT_MANUAL_EXCLUDED_PROPERTIES = ()


ROOT = Path(os.environ.get("OPENCLAW_WORKSPACE") or Path(__file__).absolute().parents[1])
DEFAULT_RUNTIME_MAP = ROOT / "reports" / "baselane_financials_monthly_lofty_pm_runtime_map.json"
DEFAULT_PATCH_SCRIPT = ROOT / "skills" / "lofty-pm" / "scripts" / "push_property_data_to_lofty.py"
DEFAULT_LIVE_FINANCIAL_CAPTURE = ROOT / "reports" / "baselane_financials_monthly_live_financial_capture.json"
DEFAULT_REVIEW_CANDIDATE_PACKET = ROOT / "reports" / "baselane_financials_monthly_review_candidate_packet.json"
DEFAULT_REPORT = ROOT / "reports" / "lofty_financial_patch_readiness.json"
DEFAULT_FINANCIAL_APPROVAL_MANIFEST = ROOT / "reports" / "lofty_financial_approval_manifest.json"
UNSAFE_PATCH_KEYS = {"updates", "updatesDiff"}
LIVE_FINANCIAL_CAPTURE_READY_STATUSES = {
    "guard_ok",
    "guard_ok_live_distribution",
    "guard_ok_no_distribution_target",
    "needs_reconcile",
}
LIVE_FINANCIAL_CAPTURE_SKIP_STATUSES = {
    "skipped_sold",
    "skipped_selling",
    "skipped_closed",
    "skipped_delisted",
}
RECONCILE_CSV_FIELDS = (
    "property_name",
    "lofty_property_id",
    "financials_md",
    "status",
    "field_count",
    "fields",
    "patch_digest",
    "live_financial_guard_status",
    "live_financial_snapshot_path",
    "next_action",
)
BLOCKED_EMPTY_CSV_FIELDS = (
    "property_name",
    "lofty_property_id",
    "financials_md",
    "approved_financials_target",
    "candidate_financial_source",
    "candidate_financial_sha256",
    "candidate_financial_quality_issues",
    "approval_target_exists",
    "approval_target_candidate_match_issues",
    "approval_copy_command_requires_current_rent_roll_and_explicit_approval",
    "required_approved_snapshot_fields",
    "next_action",
    "status",
    "issue_count",
    "issues",
    "source_quality_flags",
    "patch_builder_command",
)
BLOCKED_EMPTY_MARKDOWN_MAX_ROWS = 80
APPROVAL_DIFF_MAX_LINES = 80
BLOCKER_CSV_FIELDS = (
    "blocker_kind",
    "property_name",
    "lofty_property_id",
    "financials_md",
    "financial_candidate",
    "financial_approval_target",
    "status",
    "issues",
    "approval_target_candidate_match_issues",
    "monthly_financial_summary_issues",
    "monthly_financial_summary_missing_required_fields",
    "monthly_financial_summary_source_issues",
    "live_financial_guard_status",
    "live_financial_snapshot_path",
    "next_action",
)
REQUIRED_APPROVED_SNAPSHOT_FIELDS = (
    "ECO GL Column E net cash balance",
    "curr_maintenance_reserve held by Lofty",
    "reviewed monthly revenue",
    "reviewed monthly operating expenses",
    "source month and source file evidence",
)
DEFAULT_COO_OWNERSHIP_DISTRIBUTION_STATES = ("NY", "CA", "HI", "FL", "CO")
DEFAULT_COO_OWNERSHIP_ECO_CASH_MINIMUM = 3000.0
DEFAULT_DISTRIBUTION_DISABLED_PROPERTIES = (
    "7542 and 7656 S Colfax Ave",
    "3139 West Blvd",
)
DEFAULT_CASH_SOURCE_GUARD_DISABLED_PROPERTIES = (
    "917 Pawnee Ave",
)
GENERIC_EXCLUSION_IDENTITY_TOKENS = frozenset({"public"})


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


def coownership_distribution_state(path: object) -> str | None:
    state = property_state_from_path(path)
    if state and state in coownership_distribution_states():
        return state
    return None


def normalize_property_name(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def distribution_is_manually_disabled(property_name: object) -> bool:
    name = normalize_property_name(property_name)
    if not name:
        return False
    return any(
        disabled and (name.startswith(disabled) or disabled.startswith(name))
        for disabled in map(normalize_property_name, DEFAULT_DISTRIBUTION_DISABLED_PROPERTIES)
    )


def cash_source_guard_is_disabled(property_name: object) -> bool:
    name = normalize_property_name(property_name)
    if not name:
        return False
    return any(
        disabled and (name.startswith(disabled) or disabled.startswith(name) or disabled in name)
        for disabled in map(normalize_property_name, DEFAULT_CASH_SOURCE_GUARD_DISABLED_PROPERTIES)
    )


def combined_operating_cash_clearance(
    lofty_operating_reserve: float | None,
    eco_operating_cash: float | None,
    distribution_minimum: float | None,
) -> tuple[float | None, bool | None]:
    if lofty_operating_reserve is None or eco_operating_cash is None or distribution_minimum is None:
        return None, None
    combined = round(lofty_operating_reserve + eco_operating_cash, 2)
    return combined, combined >= distribution_minimum


def cash_source_guard_source_list(
    source_values: dict[str, float | None],
    distribution_minimum: float | None,
    below_minimum_marker: str = "combined_operating_cash_below_maintenance_reserve",
) -> tuple[list[str], float | None, bool | None]:
    eco_operating_cash = source_values.get("eco_operating_cash_net_of_accruals")
    if eco_operating_cash is None:
        eco_operating_cash = source_values.get("eco_operating_cash_full_column_e")
    combined_cash, reserve_clear = combined_operating_cash_clearance(
        source_values.get("lofty_operating_reserve"),
        eco_operating_cash,
        distribution_minimum,
    )
    if reserve_clear is True:
        return [], combined_cash, reserve_clear
    sources = [name for name, value in source_values.items() if value is not None and value <= 0]
    if reserve_clear is False and below_minimum_marker not in sources:
        sources.append(below_minimum_marker)
    return sources, combined_cash, reserve_clear


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def csv_row_count(path_value: Any) -> int | None:
    raw = str(path_value or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    try:
        with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
            return sum(1 for _ in csv.DictReader(handle))
    except OSError:
        return None


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "missing", "path": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"status": "unreadable", "path": str(path), "error": str(exc)}
    return data if isinstance(data, dict) else {"status": "unreadable", "path": str(path), "error": "not object"}


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


def stable_digest(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def approval_target_diff(candidate: object, approval_target: object, *, max_lines: int = APPROVAL_DIFF_MAX_LINES) -> list[str]:
    """Return a bounded, review-only diff for a stale financial approval target."""
    candidate_path = Path(str(candidate or ""))
    approval_path = Path(str(approval_target or ""))
    if not candidate_path.is_file() or not approval_path.is_file():
        return []
    try:
        candidate_text = candidate_path.read_text(encoding="utf-8").splitlines()
        approval_text = approval_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    diff = list(
        difflib.unified_diff(
            approval_text,
            candidate_text,
            fromfile="existing-approved",
            tofile="current-ledger-candidate",
            lineterm="",
            n=2,
        )
    )
    if len(diff) <= max_lines:
        return diff
    return [*diff[:max_lines], f"... diff truncated after {max_lines} lines"]


def report_artifact_paths(report_path: Path) -> dict[str, Path]:
    return {
        "guard_reconcile_csv": report_path.with_name(f"{report_path.stem}.guard-reconcile.csv"),
        "blocked_empty_patch_csv": report_path.with_name(f"{report_path.stem}.blocked-empty-patch.csv"),
        "blocked_empty_patch_markdown": report_path.with_name(f"{report_path.stem}.blocked-empty-patch.md"),
        "blocker_csv": report_path.with_name(f"{report_path.stem}.blockers.csv"),
        "blocker_markdown": report_path.with_name(f"{report_path.stem}.blockers.md"),
        "missing_reserve_decision_scaffold": report_path.with_name(
            f"{report_path.stem}.missing-reserve-decisions.scaffold.json"
        ),
    }


def record_digest_payload(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "property_name": record.get("property_name"),
        "lofty_property_id": record.get("lofty_property_id"),
        "financials_md": record.get("financials_md"),
        "status": record.get("status"),
        "field_count": record.get("field_count"),
        "fields": record.get("fields") or [],
        "patch_digest": record.get("patch_digest"),
        "live_financial_guard_status": record.get("live_financial_guard_status"),
        "live_financial_snapshot_path": record.get("live_financial_snapshot_path"),
    }


def financial_patch_readiness_digest(records: list[dict[str, Any]]) -> str:
    return stable_digest([record_digest_payload(record) for record in records])


def csv_value(value: Any) -> str:
    if isinstance(value, (list, dict)):
        return json.dumps(value, sort_keys=True)
    return "" if value is None else str(value)


def lookup_key(value: object) -> str:
    return str(value or "").strip().lower()


def name_tokens(value: object) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", lookup_key(value)))


def usable_exclusion_identity(value: object) -> bool:
    tokens = name_tokens(value)
    return bool(tokens and not tokens.issubset(GENERIC_EXCLUSION_IDENTITY_TOKENS))


def names_refer_to_same_property(left: object, right: object) -> bool:
    left_tokens = name_tokens(left)
    right_tokens = name_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    return left_tokens.issubset(right_tokens) or right_tokens.issubset(left_tokens)


def monthly_artifact_property_folder(path_text: object) -> str:
    path = Path(str(path_text or ""))
    if not str(path).strip():
        return ""
    property_folder = path.parent.parent
    if property_folder.name.lower() == "public":
        property_folder = property_folder.parent
    return property_folder.name


def shell_command(parts: list[object]) -> str:
    return " ".join(shlex.quote(str(part)) for part in parts if str(part or "").strip())


def candidate_packet_record_summary(record: dict[str, Any]) -> dict[str, Any]:
    monthly_summary = record.get("monthly_financial_summary") if isinstance(record.get("monthly_financial_summary"), dict) else {}
    financial_snapshot = record.get("financial_candidate_snapshot") if isinstance(record.get("financial_candidate_snapshot"), dict) else {}
    return {
        "property_name": record.get("property_name"),
        "lofty_property_id": record.get("lofty_property_id"),
        "financials_md": record.get("financials_md"),
        "financial_candidate": record.get("financial_candidate"),
        "financial_approval_target": record.get("financial_approval_target"),
        "monthly_financial_summary": monthly_summary,
        "financial_candidate_snapshot": financial_snapshot,
    }


def candidate_lookup_keys(candidate: dict[str, Any]) -> list[str]:
    return [
        lookup_key(candidate.get("lofty_property_id")),
        lookup_key(candidate.get("property_name")),
        lookup_key(candidate.get("financials_md")),
        lookup_key(candidate.get("financial_approval_target")),
        lookup_key(monthly_artifact_property_folder(candidate.get("financial_approval_target"))),
    ]


def load_financial_candidate_sources(candidate_packet_report: Path | None) -> tuple[dict[str, dict[str, Any]], list[str], dict[str, Any]]:
    if candidate_packet_report is None:
        return {}, [], {"path": None, "status": "not_configured", "candidate_count": 0}
    data = read_json(candidate_packet_report)
    if data.get("status") in {"missing", "unreadable"}:
        return {}, [f"review candidate packet {data.get('status')}: {candidate_packet_report}"], {
            "path": str(candidate_packet_report),
            "status": data.get("status"),
            "candidate_count": 0,
        }
    records = data.get("records") if isinstance(data.get("records"), list) else []
    candidates: dict[str, dict[str, Any]] = {}
    candidate_records: list[dict[str, Any]] = []
    candidate_count = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        candidate_source = str(record.get("financial_candidate") or "").strip()
        approval_target = str(record.get("financial_approval_target") or "").strip()
        if not candidate_source:
            continue
        candidate_count += 1
        candidate_records.append(candidate_packet_record_summary(record))
        candidate = {
            "candidate_financial_source": candidate_source,
            "candidate_financial_approval_target": approval_target,
            "candidate_packet_record_property_name": record.get("property_name"),
            "candidate_packet_monthly_financial_summary": record.get("monthly_financial_summary") if isinstance(record.get("monthly_financial_summary"), dict) else {},
            "candidate_packet_financial_snapshot": record.get("financial_candidate_snapshot") if isinstance(record.get("financial_candidate_snapshot"), dict) else {},
            "candidate_packet_generated_at": data.get("generated_at"),
        }
        keys = [
            record.get("lofty_property_id"),
            record.get("property_name"),
            record.get("financials_md"),
            approval_target,
            monthly_artifact_property_folder(approval_target),
        ]
        for key in keys:
            normalized = lookup_key(key)
            if normalized:
                candidates[normalized] = candidate
    return candidates, [], {
        "path": str(candidate_packet_report),
        "status": data.get("status"),
        "candidate_count": candidate_count,
        "run_month": data.get("run_month"),
        "property_count": data.get("property_count"),
        "missing_lofty_reserve_count": data.get("missing_lofty_reserve_count"),
        "missing_lofty_reserve_csv": data.get("missing_lofty_reserve_csv"),
        "missing_lofty_reserve_markdown": data.get("missing_lofty_reserve_markdown"),
        "records": candidate_records,
    }


def candidate_source_freshness_issues(snapshot: dict[str, Any]) -> list[str]:
    """Reject review candidates whose recorded source ledger no longer matches disk."""
    ledger_path = str(snapshot.get("ledger_path") or "").strip()
    if not ledger_path:
        return []
    ledger = Path(ledger_path)
    if not ledger.is_file():
        return ["candidate snapshot ledger source is missing"]
    recorded_digest = str(snapshot.get("ledger_sha256") or "").strip()
    if not recorded_digest:
        return ["candidate snapshot is missing ledger SHA-256 provenance"]
    current_digest = sha256_file(ledger)
    if current_digest != recorded_digest:
        return ["candidate snapshot ledger SHA-256 no longer matches source ledger"]
    return []


def candidate_for_record(candidates: dict[str, dict[str, Any]], record: dict[str, Any]) -> dict[str, Any] | None:
    for key in (
        record.get("lofty_property_id"),
        record.get("property_name"),
        record.get("financials_md"),
        record.get("approved_financials_target"),
        monthly_artifact_property_folder(record.get("approved_financials_target")),
    ):
        candidate = candidates.get(lookup_key(key))
        if candidate:
            return candidate
    return None


def monthly_summary_issues(summary: dict[str, Any], run_month: str | None) -> list[str]:
    issues: list[str] = []
    if not summary:
        return ["candidate packet missing monthly_financial_summary"]
    if run_month and str(summary.get("as_of_month") or "").strip() != run_month:
        issues.append(f"candidate monthly_financial_summary as_of_month is not run_month={run_month}")
    if summary.get("eco_gl_column_e_status") != "ok":
        issues.append("candidate monthly_financial_summary ECO GL Column E status is not ok")
    if summary.get("eco_gl_column_e_sum") is None:
        issues.append("candidate monthly_financial_summary missing ECO GL Column E sum")
    if summary.get("eco_gl_column_e_row_count") is None:
        issues.append("candidate monthly_financial_summary missing ECO GL Column E row count")
    if summary.get("lofty_curr_maintenance_reserve") is None:
        issues.append("candidate monthly_financial_summary missing Lofty curr_maintenance_reserve")
    if summary.get("lofty_curr_maintenance_reserve") is not None and not str(summary.get("lofty_curr_maintenance_reserve_source") or "").strip():
        issues.append("candidate monthly_financial_summary missing Lofty curr_maintenance_reserve source")
    return issues


def monthly_summary_field_coverage(summary: dict[str, Any], run_month: str | None) -> dict[str, Any]:
    required = {
        "as_of_month": bool(summary) and (not run_month or str(summary.get("as_of_month") or "").strip() == run_month),
        "eco_gl_column_e_status": bool(summary) and summary.get("eco_gl_column_e_status") == "ok",
        "eco_gl_column_e_sum": bool(summary) and summary.get("eco_gl_column_e_sum") is not None,
        "eco_gl_column_e_row_count": bool(summary) and summary.get("eco_gl_column_e_row_count") is not None,
        "lofty_curr_maintenance_reserve": bool(summary) and summary.get("lofty_curr_maintenance_reserve") is not None,
    }
    missing = [key for key, ok in required.items() if not ok]
    source_issues = []
    if bool(summary) and summary.get("lofty_curr_maintenance_reserve") is not None and not str(summary.get("lofty_curr_maintenance_reserve_source") or "").strip():
        source_issues.append("lofty_curr_maintenance_reserve_source")
    return {
        "required_field_count": len(required),
        "present_required_field_count": len(required) - len(missing),
        "missing_required_field_count": len(missing),
        "missing_required_fields": missing,
        "source_issue_count": len(source_issues),
        "source_issues": source_issues,
    }


def monthly_summary_issue_record(
    *,
    source: str,
    property_name: object,
    lofty_property_id: object,
    financials_md: object,
    financial_candidate: object,
    financial_approval_target: object,
    summary: dict[str, Any],
    run_month: str | None,
    reserve_review_csv: object = None,
    reserve_review_markdown: object = None,
) -> dict[str, Any]:
    issues = monthly_summary_issues(summary, run_month)
    coverage = monthly_summary_field_coverage(summary, run_month)
    return {
        "source": source,
        "property_name": property_name,
        "lofty_property_id": lofty_property_id,
        "financials_md": financials_md,
        "financial_candidate": financial_candidate,
        "financial_approval_target": financial_approval_target,
        "monthly_financial_summary": summary,
        "monthly_financial_summary_issues": issues,
        "missing_required_fields": coverage.get("missing_required_fields") or [],
        "source_issues": coverage.get("source_issues") or [],
        "missing_lofty_reserve_csv": reserve_review_csv,
        "missing_lofty_reserve_markdown": reserve_review_markdown,
        "next_action": blocker_next_action(
            {
                "blocker_kind": f"{source}_monthly_summary_issue",
                "monthly_financial_summary_missing_required_fields": coverage.get("missing_required_fields") or [],
                "monthly_financial_summary_source_issues": coverage.get("source_issues") or [],
            }
        ),
    }


def monthly_summary_coverage_totals(records: list[dict[str, Any]], run_month: str | None) -> dict[str, Any]:
    coverages = [
        monthly_summary_field_coverage(
            record.get("monthly_financial_summary") if isinstance(record.get("monthly_financial_summary"), dict) else {},
            run_month,
        )
        for record in records
        if isinstance(record, dict)
    ]
    required_total = sum(int(item.get("required_field_count") or 0) for item in coverages)
    present_total = sum(int(item.get("present_required_field_count") or 0) for item in coverages)
    missing_total = sum(int(item.get("missing_required_field_count") or 0) for item in coverages)
    source_issue_total = sum(int(item.get("source_issue_count") or 0) for item in coverages)
    missing_by_field = {
        field: sum(1 for item in coverages if field in (item.get("missing_required_fields") or []))
        for field in ("as_of_month", "eco_gl_column_e_status", "eco_gl_column_e_sum", "eco_gl_column_e_row_count", "lofty_curr_maintenance_reserve")
    }
    return {
        "record_count": len(coverages),
        "required_field_total": required_total,
        "present_required_field_total": present_total,
        "missing_required_field_total": missing_total,
        "source_issue_total": source_issue_total,
        "coverage_ratio": round(present_total / required_total, 6) if required_total else 1.0,
        "missing_required_by_field": {key: value for key, value in missing_by_field.items() if value},
    }


def candidate_financial_quality_issues(path: Path) -> list[str]:
    if not path.is_file():
        return ["candidate_financial_source_missing"]
    text = path.read_text(encoding="utf-8", errors="replace")
    lower_text = text.lower()
    issues: list[str] = []
    has_ledger_summary = re.search(r"^##\s+ledger summary\s*$", text, flags=re.MULTILINE | re.IGNORECASE) is not None
    if has_ledger_summary and "review before investor email/publish" in lower_text:
        issues.append("candidate_generated_ledger_summary_review_required")
    elif has_ledger_summary:
        issues.append("candidate_generated_ledger_summary_only")
    if "no reviewed markdown `financials.md` source existed yet" in lower_text:
        issues.append("candidate_generated_without_reviewed_financials_source")
    if "Lofty Operating Cash" not in text:
        issues.append("missing_lofty_operating_cash")
    if "ECO Operating Cash" not in text:
        issues.append("missing_eco_operating_cash")
    if "curr_maintenance_reserve" not in text:
        issues.append("missing_curr_maintenance_reserve_source_label")
    if "ECO Systems General Ledger Column E" not in text:
        issues.append("missing_eco_gl_column_e_source_label")
    parseable_patch_markers = (
        "## Cash Flow Snapshot",
        "## Income",
        "## Operating Expenses",
        "## Cash Flow",
        "## Operating P&L",
        "## Annual Expenses",
    )
    if not any(marker.lower() in lower_text for marker in parseable_patch_markers):
        issues.append("missing_parseable_lofty_financial_patch_section")
    return issues


def approval_target_quality_issues(path: Path) -> list[str]:
    if not path.is_file():
        return ["approval_target_missing"]
    return [
        issue.replace("candidate_", "approval_target_", 1)
        for issue in candidate_financial_quality_issues(path)
    ]


def approval_target_candidate_match_issues(candidate_path: Path, approval_target: Path) -> list[str]:
    """Require an approval target to be the exact current review candidate.

    A month-stamped approval can remain syntactically valid after its ledger-backed
    candidate is regenerated.  It must not silently authorize publication of the
    older financial snapshot.
    """
    if not candidate_path.is_file() or not approval_target.is_file():
        return []
    if sha256_file(candidate_path) != sha256_file(approval_target):
        return ["approval_target_does_not_match_current_financial_candidate"]
    return []


def approval_copy_command(candidate_source: str, approval_target: str) -> str:
    return shell_command(
        [
            "bash",
            "-lc",
            (
                f"test -d {shlex.quote(str(Path(approval_target).parent))} && "
                f"test ! -e {shlex.quote(approval_target)} && "
                f"cp -- {shlex.quote(candidate_source)} {shlex.quote(approval_target)}"
            ),
        ]
    )


def should_emit_approval_copy_command(record: dict[str, Any], approval_target: str) -> bool:
    if not str(record.get("candidate_financial_source") or "").strip() or not approval_target:
        return False
    if record.get("candidate_financial_quality_issues"):
        return False
    return record.get("approval_target_exists") is False


def write_csv(path: Path, rows: list[dict[str, Any]], fields: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: csv_value(row.get(field)) for field in fields})


def write_missing_reserve_decision_scaffold(
    path: Path,
    issue_records: list[dict[str, Any]],
    *,
    run_month: str | None,
) -> list[dict[str, Any]]:
    decisions = []
    seen: set[tuple[str, str, str]] = set()
    for record in issue_records:
        if not isinstance(record, dict):
            continue
        missing_fields = record.get("missing_required_fields") if isinstance(record.get("missing_required_fields"), list) else []
        if "lofty_curr_maintenance_reserve" not in missing_fields:
            continue
        key = (
            str(record.get("source") or ""),
            str(record.get("property_name") or ""),
            str(record.get("financials_md") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        decisions.append(
            {
                "property_name": record.get("property_name"),
                "lofty_property_id": record.get("lofty_property_id"),
                "source": record.get("source"),
                "run_month": run_month,
                "financials_md": record.get("financials_md"),
                "financial_candidate": record.get("financial_candidate"),
                "financial_approval_target": record.get("financial_approval_target"),
                "missing_required_fields": missing_fields,
                "curr_maintenance_reserve": None,
                "curr_maintenance_reserve_source": "",
                "reviewed": False,
                "reviewed_at": "",
                "reviewed_by": "",
                "decision": "populate_curr_maintenance_reserve",
                "notes": "",
            }
        )
    payload = {
        "generated_at": iso_z(),
        "run_month": run_month,
        "decision": "populate_missing_lofty_curr_maintenance_reserve",
        "reviewed": False,
        "instructions": (
            "Fill curr_maintenance_reserve and source for each row from live Lofty listing/profile evidence, "
            "set reviewed=true only after human review, then regenerate monthly review candidates. "
            "This scaffold does not mutate FINANCIALS.md."
        ),
        "record_count": len(decisions),
        "records": decisions,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return decisions


def approved_financials_target(financials_md: str | None, run_month: str | None) -> str | None:
    if not financials_md or not run_month:
        return None
    path = Path(str(financials_md))
    return str(path.with_name(f"{run_month}-FINANCIALS-approved.md"))


def required_approved_snapshot_fields_text() -> str:
    return "; ".join(REQUIRED_APPROVED_SNAPSHOT_FIELDS)


def blocked_empty_next_action(row: dict[str, Any]) -> str:
    target = row.get("approved_financials_target") or "month-stamped approved FINANCIALS snapshot"
    if row.get("approval_target_exists") is True and row.get("approval_target_quality_issues"):
        return (
            f"Replace/review unsafe existing {target}; it is not an apply-safe approved snapshot. "
            "Required fields: ECO GL Column E net cash balance, curr_maintenance_reserve held by Lofty, "
            "reviewed monthly Revenue/OpEx, and source evidence."
        )
    return (
        f"Create/review {target} with ECO GL Column E net cash balance, curr_maintenance_reserve held by Lofty, "
        "reviewed monthly Revenue/OpEx, and source evidence before any Lofty PM financial apply."
    )


def infer_run_month(live_capture: dict[str, Any], records: list[dict[str, Any]]) -> str | None:
    explicit = str(live_capture.get("run_month") or "").strip()
    if explicit:
        return explicit
    candidates: list[str] = []
    for record in records:
        for key in ("live_financial_snapshot_path", "financials_md"):
            value = str(record.get(key) or "")
            candidates.extend(re.findall(r"/(20\d{2}-\d{2})/", value))
    return candidates[0] if candidates else None


def write_blocked_empty_markdown(path: Path, rows: list[dict[str, Any]], *, run_month: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Lofty Financial Patch Review Packet",
        "",
        f"- Run month: {run_month or 'unknown'}",
        f"- Blocked empty patch count: {len(rows)}",
        "- External mutation: none; this packet is local review evidence only.",
        "- Required action: create or review the month-stamped approved FINANCIALS snapshot before any Lofty PM financial apply.",
        "- Approval-copy commands require current rent-roll evidence and explicit operator approval; they are not run by this report.",
        "- Required approved snapshot fields:",
        *[f"  - {field}" for field in REQUIRED_APPROVED_SNAPSHOT_FIELDS],
        "",
    ]
    if not rows:
        lines.append("No blocked empty financial patches.")
    else:
        for index, row in enumerate(rows[:BLOCKED_EMPTY_MARKDOWN_MAX_ROWS], start=1):
            lines.extend(
                [
                    f"## {index}. {row.get('property_name') or 'Unknown property'}",
                    "",
                    f"- Lofty property ID: `{row.get('lofty_property_id') or ''}`",
                    f"- Source FINANCIALS.md: `{row.get('financials_md') or ''}`",
                    f"- Candidate financial source: `{row.get('candidate_financial_source') or ''}`",
                    f"- Candidate financial SHA256: `{row.get('candidate_financial_sha256') or ''}`",
                    f"- Approved target: `{row.get('approved_financials_target') or ''}`",
                    f"- Approval target exists: `{row.get('approval_target_exists')}`",
                    f"- Approval target quality issues: {', '.join(row.get('approval_target_quality_issues') or []) if isinstance(row.get('approval_target_quality_issues'), list) else row.get('approval_target_quality_issues') or ''}",
                    f"- Approval target candidate match issues: {', '.join(row.get('approval_target_candidate_match_issues') or []) if isinstance(row.get('approval_target_candidate_match_issues'), list) else row.get('approval_target_candidate_match_issues') or ''}",
                    f"- Candidate quality issues: {', '.join(row.get('candidate_financial_quality_issues') or []) if isinstance(row.get('candidate_financial_quality_issues'), list) else row.get('candidate_financial_quality_issues') or ''}",
                    f"- Next action: {row.get('next_action') or blocked_empty_next_action(row)}",
                    f"- Issues: {', '.join(row.get('issues') or []) if isinstance(row.get('issues'), list) else row.get('issues') or ''}",
                    "",
                ]
            )
        if len(rows) > BLOCKED_EMPTY_MARKDOWN_MAX_ROWS:
            lines.append(f"... {len(rows) - BLOCKED_EMPTY_MARKDOWN_MAX_ROWS} additional rows omitted; see CSV.")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def blocker_next_action(row: dict[str, Any]) -> str:
    kind = row.get("blocker_kind")
    missing_fields = row.get("monthly_financial_summary_missing_required_fields")
    missing_fields = missing_fields if isinstance(missing_fields, list) else []
    source_issues = row.get("monthly_financial_summary_source_issues")
    source_issues = source_issues if isinstance(source_issues, list) else []
    if kind == "candidate_missing_runtime":
        return "Add this property to the Lofty PM runtime map or mark it explicitly unavailable/sold before live listing publish."
    if kind == "candidate_monthly_summary_issue":
        if "lofty_curr_maintenance_reserve" in missing_fields:
            return "Populate Lofty curr_maintenance_reserve from the live Lofty listing/reserve source, then regenerate the monthly review candidate packet."
        if source_issues:
            return "Populate missing monthly financial summary source evidence, then regenerate the monthly review candidate packet."
        return "Repair monthly FINANCIALS.md candidate source so ECO GL Column E and Lofty curr_maintenance_reserve are present for the run month."
    if kind == "runtime_monthly_summary_issue":
        if "lofty_curr_maintenance_reserve" in missing_fields:
            return "Populate Lofty curr_maintenance_reserve from the live Lofty listing/reserve source, then regenerate the monthly review candidate packet for this runtime property."
        if source_issues:
            return "Populate missing monthly financial summary source evidence, then regenerate the monthly review candidate packet for this runtime property."
        return "Regenerate the monthly review candidate packet from current FINANCIALS.md and Lofty reserve data for this runtime property."
    if kind == "missing_live_financial_capture":
        if row.get("approval_target_candidate_match_issues"):
            return (
                "Have a finance reviewer approve the current candidate into the month-stamped approval target, "
                "then capture/reconcile live FINANCIALS.md guard evidence before live financial apply."
            )
        return "Capture/reconcile live FINANCIALS.md guard evidence for this Lofty runtime property before live financial apply."
    if kind == "approval_target_stale":
        return (
            "Have a finance reviewer compare the current ledger-backed candidate with the month-stamped approval "
            "target and explicitly approve the current candidate before live financial apply."
        )
    return "Review this Lofty financial patch readiness blocker before live listing publish."


def write_blocker_markdown(path: Path, rows: list[dict[str, Any]], *, run_month: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Lofty Financial Patch Readiness Blockers",
        "",
        f"- Run month: {run_month or 'unknown'}",
        f"- Blocker count: {len(rows)}",
        "- External mutation: none; this packet is local review evidence only.",
        "",
    ]
    if not rows:
        lines.append("No Lofty financial patch readiness blockers.")
    else:
        for index, row in enumerate(rows, start=1):
            monthly_issues = row.get("monthly_financial_summary_issues")
            issues = row.get("issues")
            missing_required_fields = row.get("monthly_financial_summary_missing_required_fields")
            source_issues = row.get("monthly_financial_summary_source_issues")
            approval_target_candidate_match_issues = row.get("approval_target_candidate_match_issues")
            lines.extend(
                [
                    f"## {index}. {row.get('property_name') or 'Unknown property'}",
                    "",
                    f"- Blocker kind: `{row.get('blocker_kind')}`",
                    f"- Lofty property ID: `{row.get('lofty_property_id') or ''}`",
                    f"- FINANCIALS.md: `{row.get('financials_md') or ''}`",
                    f"- Financial candidate: `{row.get('financial_candidate') or ''}`",
                    f"- Financial approval target: `{row.get('financial_approval_target') or ''}`",
                    f"- Status: `{row.get('status') or ''}`",
                    f"- Issues: {', '.join(issues) if isinstance(issues, list) else issues or ''}",
                    f"- Approval target candidate match issues: {', '.join(approval_target_candidate_match_issues) if isinstance(approval_target_candidate_match_issues, list) else approval_target_candidate_match_issues or ''}",
                    f"- Monthly summary issues: {', '.join(monthly_issues) if isinstance(monthly_issues, list) else monthly_issues or ''}",
                    f"- Missing required monthly fields: {', '.join(missing_required_fields) if isinstance(missing_required_fields, list) else missing_required_fields or ''}",
                    f"- Monthly source issues: {', '.join(source_issues) if isinstance(source_issues, list) else source_issues or ''}",
                    f"- Missing Lofty reserve CSV: `{row.get('missing_lofty_reserve_csv') or ''}`",
                    f"- Missing Lofty reserve review: `{row.get('missing_lofty_reserve_markdown') or ''}`",
                    f"- Live financial guard status: `{row.get('live_financial_guard_status') or ''}`",
                    f"- Live financial snapshot: `{row.get('live_financial_snapshot_path') or ''}`",
                    f"- Next action: {row.get('next_action') or blocker_next_action(row)}",
                    "",
                ]
            )
            approval_diff = row.get("approval_target_diff")
            if isinstance(approval_diff, list) and approval_diff:
                lines.extend(["```diff", *[str(line) for line in approval_diff], "```", ""])
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_review_artifacts(report: dict[str, Any], report_path: Path) -> dict[str, Any]:
    paths = report_artifact_paths(report_path)
    records = report.get("records") if isinstance(report.get("records"), list) else []
    run_month = str(report.get("run_month") or "").strip() or None
    reconcile_rows = []
    blocked_empty_rows = []
    blocker_rows = []
    for row in report.get("candidate_packet_missing_runtime_records") if isinstance(report.get("candidate_packet_missing_runtime_records"), list) else []:
        if isinstance(row, dict):
            blocker = {
                **row,
                "blocker_kind": "candidate_missing_runtime",
                "status": "candidate_missing_runtime",
                "issues": [],
                "next_action": blocker_next_action({"blocker_kind": "candidate_missing_runtime"}),
            }
            blocker_rows.append(blocker)
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("status") == "patch_ready_guard_reconcile_required":
            reconcile_rows.append(
                {
                    **record,
                    "next_action": "Recapture/reconcile the live FINANCIALS.md guard for this property before any Lofty PM financial listing apply.",
                }
            )
        elif record.get("status") == "blocked_approval_target_stale":
            blocker_row = {
                "blocker_kind": "approval_target_stale",
                "property_name": record.get("property_name"),
                "lofty_property_id": record.get("lofty_property_id"),
                "financials_md": record.get("financials_md"),
                "financial_candidate": record.get("candidate_financial_source"),
                "financial_approval_target": record.get("approved_financials_target"),
                "status": record.get("status"),
                "issues": record.get("issues") if isinstance(record.get("issues"), list) else [],
                "approval_target_candidate_match_issues": (
                    record.get("approval_target_candidate_match_issues")
                    if isinstance(record.get("approval_target_candidate_match_issues"), list)
                    else []
                ),
                "monthly_financial_summary_issues": (
                    record.get("candidate_packet_monthly_financial_summary_issues")
                    if isinstance(record.get("candidate_packet_monthly_financial_summary_issues"), list)
                    else []
                ),
                "monthly_financial_summary_missing_required_fields": (
                    (record.get("candidate_packet_monthly_financial_summary_field_coverage") or {}).get("missing_required_fields")
                    if isinstance(record.get("candidate_packet_monthly_financial_summary_field_coverage"), dict)
                    else []
                ),
                "monthly_financial_summary_source_issues": (
                    (record.get("candidate_packet_monthly_financial_summary_field_coverage") or {}).get("source_issues")
                    if isinstance(record.get("candidate_packet_monthly_financial_summary_field_coverage"), dict)
                    else []
                ),
                "live_financial_guard_status": record.get("live_financial_guard_status"),
                "live_financial_snapshot_path": record.get("live_financial_snapshot_path"),
            }
            blocker_row["next_action"] = blocker_next_action(blocker_row)
            blocker_row["approval_target_diff"] = approval_target_diff(
                blocker_row["financial_candidate"],
                blocker_row["financial_approval_target"],
            )
            blocker_rows.append(blocker_row)
        elif record.get("status") == "blocked_empty_patch":
            approved_target = record.get("approved_financials_target") or approved_financials_target(record.get("financials_md"), run_month)
            row = {
                **record,
                "approved_financials_target": approved_target,
                "required_approved_snapshot_fields": required_approved_snapshot_fields_text(),
            }
            candidate_source = str(row.get("candidate_financial_source") or "").strip()
            row.pop("approval_copy_command_requires_current_rent_roll_and_explicit_approval", None)
            if should_emit_approval_copy_command(row, str(approved_target or "")):
                row["approval_copy_command_requires_current_rent_roll_and_explicit_approval"] = approval_copy_command(candidate_source, str(approved_target))
            row["next_action"] = blocked_empty_next_action(row)
            blocked_empty_rows.append(
                row
            )
        monthly_issues = record.get("candidate_packet_monthly_financial_summary_issues")
        if isinstance(monthly_issues, list) and monthly_issues:
            blocker_row = {
                "blocker_kind": "runtime_monthly_summary_issue",
                "property_name": record.get("property_name"),
                "lofty_property_id": record.get("lofty_property_id"),
                "financials_md": record.get("financials_md"),
                "financial_candidate": record.get("candidate_financial_source"),
                "financial_approval_target": record.get("approved_financials_target"),
                "status": record.get("status"),
                "issues": record.get("issues") if isinstance(record.get("issues"), list) else [],
                "monthly_financial_summary_issues": monthly_issues,
                "monthly_financial_summary_missing_required_fields": (
                    (record.get("candidate_packet_monthly_financial_summary_field_coverage") or {}).get("missing_required_fields")
                    if isinstance(record.get("candidate_packet_monthly_financial_summary_field_coverage"), dict)
                    else []
                ),
                "monthly_financial_summary_source_issues": (
                    (record.get("candidate_packet_monthly_financial_summary_field_coverage") or {}).get("source_issues")
                    if isinstance(record.get("candidate_packet_monthly_financial_summary_field_coverage"), dict)
                    else []
                ),
                "missing_lofty_reserve_csv": report.get("missing_lofty_reserve_csv"),
                "missing_lofty_reserve_markdown": report.get("missing_lofty_reserve_markdown"),
                "live_financial_guard_status": record.get("live_financial_guard_status"),
                "live_financial_snapshot_path": record.get("live_financial_snapshot_path"),
            }
            blocker_row["next_action"] = blocker_next_action(blocker_row)
            blocker_rows.append(blocker_row)
        if record.get("status") == "blocked_missing_live_financial_capture":
            blocker_rows.append(
                {
                    "blocker_kind": "missing_live_financial_capture",
                    "property_name": record.get("property_name"),
                    "lofty_property_id": record.get("lofty_property_id"),
                    "financials_md": record.get("financials_md"),
                    "financial_candidate": record.get("candidate_financial_source"),
                    "financial_approval_target": record.get("approved_financials_target"),
                    "status": record.get("status"),
                    "issues": record.get("issues") if isinstance(record.get("issues"), list) else [],
                    "monthly_financial_summary_issues": record.get("candidate_packet_monthly_financial_summary_issues") if isinstance(record.get("candidate_packet_monthly_financial_summary_issues"), list) else [],
                    "monthly_financial_summary_missing_required_fields": (
                        (record.get("candidate_packet_monthly_financial_summary_field_coverage") or {}).get("missing_required_fields")
                        if isinstance(record.get("candidate_packet_monthly_financial_summary_field_coverage"), dict)
                        else []
                    ),
                    "live_financial_guard_status": record.get("live_financial_guard_status"),
                    "live_financial_snapshot_path": record.get("live_financial_snapshot_path"),
                    "next_action": blocker_next_action({"blocker_kind": "missing_live_financial_capture"}),
                }
            )
    write_csv(paths["guard_reconcile_csv"], reconcile_rows, RECONCILE_CSV_FIELDS)
    write_csv(paths["blocked_empty_patch_csv"], blocked_empty_rows, BLOCKED_EMPTY_CSV_FIELDS)
    write_blocked_empty_markdown(paths["blocked_empty_patch_markdown"], blocked_empty_rows, run_month=run_month)
    write_csv(paths["blocker_csv"], blocker_rows, BLOCKER_CSV_FIELDS)
    write_blocker_markdown(paths["blocker_markdown"], blocker_rows, run_month=run_month)
    missing_reserve_issue_records = []
    for key in ("candidate_packet_monthly_summary_issue_records", "runtime_monthly_summary_issue_records"):
        rows = report.get(key)
        if isinstance(rows, list):
            missing_reserve_issue_records.extend(row for row in rows if isinstance(row, dict))
    missing_reserve_decisions = write_missing_reserve_decision_scaffold(
        paths["missing_reserve_decision_scaffold"],
        missing_reserve_issue_records,
        run_month=run_month,
    )
    report["guard_reconcile_csv"] = str(paths["guard_reconcile_csv"])
    report["blocked_empty_patch_csv"] = str(paths["blocked_empty_patch_csv"])
    report["blocked_empty_patch_markdown"] = str(paths["blocked_empty_patch_markdown"])
    report["blocker_csv"] = str(paths["blocker_csv"])
    report["blocker_markdown"] = str(paths["blocker_markdown"])
    report["missing_lofty_reserve_decision_scaffold"] = str(paths["missing_reserve_decision_scaffold"])
    report["missing_lofty_reserve_decision_scaffold_record_count"] = len(missing_reserve_decisions)
    report["guard_reconcile_csv_count"] = len(reconcile_rows)
    report["blocked_empty_patch_csv_count"] = len(blocked_empty_rows)
    report["blocker_csv_count"] = len(blocker_rows)
    return report


def record_has_generated_ledger_review_flag(record: dict[str, Any]) -> bool:
    flags = record.get("source_quality_flags") if isinstance(record.get("source_quality_flags"), list) else []
    return "ledger_summary_generated_review_required" in flags


def extract_json(stdout: str) -> dict[str, Any]:
    text = (stdout or "").strip()
    if not text:
        return {"status": "unreadable", "error": "empty stdout"}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return {"status": "unreadable", "error": "stdout does not contain JSON object"}
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            return {"status": "unreadable", "error": f"stdout JSON parse failed: {exc}"}
    return data if isinstance(data, dict) else {"status": "unreadable", "error": "stdout JSON is not object"}


def live_financial_statuses(path: Path | None) -> tuple[dict[str, dict[str, Any]], list[str]]:
    if path is None:
        return {}, []
    data = read_json(path)
    if data.get("status") in {"missing", "unreadable"}:
        return {}, [f"live financial capture report {data.get('status')}: {path}"]
    records = data.get("records") if isinstance(data.get("records"), list) else []
    externally_excluded_records = data.get("externally_excluded_records") if isinstance(data.get("externally_excluded_records"), list) else []
    skipped_records = data.get("skipped_records") if isinstance(data.get("skipped_records"), list) else []
    skipped_index_records = data.get("skipped_index_records") if isinstance(data.get("skipped_index_records"), list) else []
    statuses: dict[str, dict[str, Any]] = {}
    for record in [*records, *externally_excluded_records, *skipped_records, *skipped_index_records]:
        if not isinstance(record, dict):
            continue
        check = record.get("check") if isinstance(record.get("check"), dict) else {}
        raw_status = str(record.get("status") or record.get("raw_status") or "").strip()
        is_skip_status = raw_status in LIVE_FINANCIAL_CAPTURE_SKIP_STATUSES or raw_status.startswith("excluded_")
        status = {
            "status": raw_status,
            "guard_ok": record.get("status") == "guard_ok" and check.get("ok") is True,
            "check_return_code": check.get("return_code"),
            "live_financials_length": record.get("live_financials_length"),
            "snapshot_path": record.get("snapshot_path") or record.get("next_action_file"),
            "next_action_stage": record.get("next_action_stage"),
            "skipped": is_skip_status,
            "live_distribution_verify": record.get("live_distribution_verify")
            if isinstance(record.get("live_distribution_verify"), dict)
            else {},
        }
        for key in (
            str(record.get("lofty_property_id") or "").strip(),
            str(record.get("financials_md") or "").strip(),
            str(record.get("property_name") or "").strip().lower(),
            str(record.get("input_property_path") or "").strip(),
            str(record.get("property_path") or "").strip(),
            str(record.get("matched_exclusion_property") or "").strip().lower(),
        ):
            if key:
                statuses[key] = status
    return statuses, []


def live_financial_excluded_names(path: Path | None) -> list[str]:
    if path is None:
        return []
    data = read_json(path)
    if data.get("status") in {"missing", "unreadable"}:
        return []
    names: list[str] = []
    for key in ("excluded_property_names", "manual_excluded_property_names"):
        values = data.get(key) if isinstance(data.get(key), list) else []
        names.extend(str(value) for value in values if str(value or "").strip())
    for key in ("externally_excluded_records", "skipped_records", "skipped_index_records"):
        records = data.get(key) if isinstance(data.get(key), list) else []
        for record in records:
            if not isinstance(record, dict):
                continue
            names.extend(
                str(value)
                for value in (
                    record.get("property_name"),
                    record.get("matched_exclusion_property"),
                    record.get("property_path"),
                    record.get("input_property_path"),
                )
                if str(value or "").strip()
            )
    deduped: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = lookup_key(name)
        if key and usable_exclusion_identity(name) and key not in seen:
            deduped.append(name)
            seen.add(key)
    return deduped


def configured_excluded_names(path: Path | None) -> list[str]:
    names = [*live_financial_excluded_names(path), *DEFAULT_MANUAL_EXCLUDED_PROPERTIES]
    deduped: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = lookup_key(name)
        if key and key not in seen:
            deduped.append(str(name))
            seen.add(key)
    return deduped


def candidate_is_excluded(candidate: dict[str, Any], excluded_names: list[str]) -> bool:
    candidate_names = [
        candidate.get("property_name"),
        candidate.get("financials_md"),
        candidate.get("financial_approval_target"),
        monthly_artifact_property_folder(candidate.get("financial_approval_target")),
    ]
    return any(
        names_refer_to_same_property(candidate_name, excluded_name)
        for candidate_name in candidate_names
        for excluded_name in excluded_names
    )


def runtime_property_is_excluded(prop: dict[str, Any], excluded_names: list[str]) -> bool:
    prop_names = [
        prop.get("property_name"),
        prop.get("full_address"),
        prop.get("financials_md"),
        monthly_artifact_property_folder(prop.get("financials_md")),
    ]
    return any(
        names_refer_to_same_property(prop_name, excluded_name)
        for prop_name in prop_names
        for excluded_name in excluded_names
    )


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
    patch: dict[str, Any],
) -> bool:
    if not isinstance(live_status, dict):
        return False
    if live_status.get("status") != "blocked_live_distribution_mismatch":
        return False
    if "cash_flow" not in patch:
        return False
    verify = live_status.get("live_distribution_verify")
    if not isinstance(verify, dict) or verify.get("targeted") is not True:
        return False
    try:
        live_length = int(live_status.get("live_financials_length") or 0)
    except (TypeError, ValueError):
        live_length = 0
    return live_length > 0 and bool(str(live_status.get("snapshot_path") or "").strip())


def distribution_guard_preview(
    summary: dict[str, Any] | None,
    live_status: dict[str, Any] | None,
    patch: dict[str, Any],
    financials_md: object = None,
    property_name: object = None,
) -> dict[str, Any]:
    summary = summary if isinstance(summary, dict) else {}
    coownership_state = coownership_distribution_state(financials_md)
    reserve_policy_property = canonical_reserve_property(property_name) or canonical_reserve_property(
        monthly_artifact_property_folder(financials_md)
    )
    maintenance_reserve = float(MAINTENANCE_RESERVE_TARGET) if reserve_policy_property else coownership_eco_cash_minimum()
    distribution_minimum = float(FULL_REPLENISHMENT_THRESHOLD) if reserve_policy_property else maintenance_reserve
    source_values = {
        "lofty_operating_reserve": parse_accounting_money(summary.get("lofty_curr_maintenance_reserve")),
        (
            "eco_operating_cash_net_of_accruals"
            if reserve_policy_property
            else "eco_operating_cash_full_column_e"
        ): parse_accounting_money(
            summary.get("eco_gl_column_e_net_of_accruals")
            if reserve_policy_property
            else summary.get("eco_gl_column_e_sum")
        ),
    }
    missing_sources = [name for name, value in source_values.items() if value is None]
    cash_source_guard_sources, combined_operating_cash, distribution_minimum_clear = cash_source_guard_source_list(
        source_values,
        distribution_minimum,
        (
            "combined_operating_cash_below_distribution_minimum"
            if reserve_policy_property
            else "combined_operating_cash_below_maintenance_reserve"
        ),
    )
    if reserve_policy_property and summary:
        for missing_source in missing_sources:
            if missing_source not in cash_source_guard_sources:
                cash_source_guard_sources.append(missing_source)
    maintenance_reserve_clear = (
        combined_operating_cash >= maintenance_reserve
        if combined_operating_cash is not None and maintenance_reserve is not None
        else None
    )
    cash_source_guard_disabled = cash_source_guard_is_disabled(property_name)
    if cash_source_guard_disabled:
        cash_source_guard_sources = []
    manual_distribution_disable = distribution_is_manually_disabled(property_name)
    zero_distribution_sources = list(cash_source_guard_sources)
    if manual_distribution_disable:
        zero_distribution_sources.append("manual_distribution_disable")
    verify = live_status.get("live_distribution_verify") if isinstance(live_status, dict) else {}
    verify = verify if isinstance(verify, dict) else {}
    live_cash_flow_actual = parse_accounting_money(verify.get("actual"))
    live_coc_actual = parse_accounting_money(verify.get("actual_coc"))
    live_projected_rental_yield_actual = parse_accounting_money(verify.get("actual_projected_rental_yield"))
    live_is_occupied_actual = verify.get("actual_is_occupied")
    live_unit_annual_cash_flow = parse_accounting_money(verify.get("live_cashflow_per_unit_annual_cash_flow"))
    patch_cash_flow = parse_accounting_money(patch.get("cash_flow"))
    patch_coc = parse_accounting_money(patch.get("coc"))
    patch_yield = parse_accounting_money(patch.get("projected_rental_yield"))
    patch_annual_cash_flow = parse_accounting_money(patch.get("projected_annual_cash_flow"))
    if patch_annual_cash_flow is not None and patch_annual_cash_flow <= 0:
        zero_distribution_sources.append("nonpositive_projected_annual_cash_flow")
    guard_active = bool(zero_distribution_sources)
    live_positive_cash_flow = bool(live_cash_flow_actual is not None and live_cash_flow_actual > 0)
    live_positive_coc = bool(live_coc_actual is not None and live_coc_actual > 0)
    live_positive_projected_rental_yield = bool(
        live_projected_rental_yield_actual is not None and live_projected_rental_yield_actual > 0
    )
    live_distribution_enabled = live_is_occupied_actual is True
    capture_distribution_mismatch = any(
        verify.get(flag) is False
        for flag in ("cash_flow_ok", "coc_ok", "projected_rental_yield_ok", "is_occupied_ok")
    )
    live_distribution_mismatch = capture_distribution_mismatch or (
        guard_active
        and (
            live_positive_cash_flow
            or live_positive_coc
            or live_positive_projected_rental_yield
            or live_distribution_enabled
        )
    )
    patch_positive_cash_flow = bool(patch_cash_flow is not None and patch_cash_flow > 0)
    patch_positive_coc = bool(patch_coc is not None and patch_coc > 0)
    patch_positive_yield = bool(patch_yield is not None and patch_yield > 0)
    if guard_active:
        guarded_patch_cash_flow = 0.0
        guarded_patch_cash_flow_source = "distribution_guard_zero"
    elif live_unit_annual_cash_flow is not None:
        guarded_patch_cash_flow = round(max(live_unit_annual_cash_flow, 0.0), 2)
        guarded_patch_cash_flow_source = "live_cashflow_per_unit_sum"
    elif patch_annual_cash_flow is not None:
        guarded_patch_cash_flow = round(max(patch_annual_cash_flow, 0.0), 2)
        guarded_patch_cash_flow_source = "patch_projected_annual_cash_flow"
    else:
        guarded_patch_cash_flow = patch_cash_flow
        guarded_patch_cash_flow_source = "patch_cash_flow"
    guarded_patch_coc = 0.0 if guard_active else patch_coc
    guarded_patch_yield = 0.0 if guard_active else patch_yield
    issues: list[str] = []
    if (
        guard_active
        and (
            live_positive_cash_flow
            or live_positive_coc
            or live_positive_projected_rental_yield
            or live_distribution_enabled
        )
    ):
        issues.append(
            "distribution guard requires zero cash_flow/coc/projected_rental_yield and disabled distributions because cash-source guard, nonpositive annual cash flow, or manual disable is active"
        )
    return {
        "status": "guard_active" if guard_active else "guard_clear",
        "source_values": source_values,
        "coownership_distribution_state": coownership_state,
        "coownership_eco_cash_minimum": distribution_minimum if coownership_state else None,
        "maintenance_reserve": maintenance_reserve,
        "distribution_minimum": distribution_minimum,
        "reserve_policy_property": reserve_policy_property,
        "combined_operating_cash": combined_operating_cash,
        "combined_operating_cash_clears_distribution_minimum": distribution_minimum_clear,
        "combined_operating_cash_clears_maintenance_reserve": maintenance_reserve_clear,
        "or_replenishment_rate": (
            float(replenishment_rate(Decimal(str(combined_operating_cash))))
            if reserve_policy_property and combined_operating_cash is not None
            else None
        ),
        "missing_sources": missing_sources,
        "cash_source_guard_sources": cash_source_guard_sources,
        "cash_source_guard_disabled": cash_source_guard_disabled,
        "zero_distribution_sources": zero_distribution_sources,
        "manual_distribution_disable": manual_distribution_disable,
        "requires_zero_distribution": guard_active,
        "live_cash_flow_actual": live_cash_flow_actual,
        "live_coc_actual": live_coc_actual,
        "live_projected_rental_yield_actual": live_projected_rental_yield_actual,
        "live_is_occupied_actual": live_is_occupied_actual,
        "live_cashflow_per_unit_annual_cash_flow": live_unit_annual_cash_flow,
        "live_positive_cash_flow": live_positive_cash_flow,
        "live_positive_coc": live_positive_coc,
        "live_positive_projected_rental_yield": live_positive_projected_rental_yield,
        "live_distribution_enabled": live_distribution_enabled,
        "live_distribution_mismatch": live_distribution_mismatch,
        "patch_cash_flow": patch_cash_flow,
        "patch_coc": patch_coc,
        "patch_projected_rental_yield": patch_yield,
        "patch_projected_annual_cash_flow": patch_annual_cash_flow,
        "raw_patch_positive_cash_flow": patch_positive_cash_flow,
        "raw_patch_positive_coc": patch_positive_coc,
        "raw_patch_positive_projected_rental_yield": patch_positive_yield,
        "guarded_patch_cash_flow": guarded_patch_cash_flow,
        "guarded_patch_cash_flow_source": guarded_patch_cash_flow_source,
        "guarded_patch_coc": guarded_patch_coc,
        "guarded_patch_projected_rental_yield": guarded_patch_yield,
        "guarded_patch_policy": "cash-source guard, manual disable, or nonpositive reviewed annual cash flow rewrites raw patch cash_flow, coc, and projected_rental_yield to 0 before any live write; when clear, Lofty API cash_flow equals guarded Annual Cash Flow and the UI Current Month Distribution is cash_flow divided by 12",
        "issues": issues,
    }


def live_financial_capture_skipped(live_status: dict[str, Any] | None) -> bool:
    return isinstance(live_status, dict) and live_status.get("skipped") is True


def runtime_properties(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    data = read_json(path)
    if data.get("status") in {"missing", "unreadable"}:
        return [], [f"runtime map {data.get('status')}: {path}"]
    props = data.get("properties") if isinstance(data.get("properties"), list) else []
    return [prop for prop in props if isinstance(prop, dict)], []


def property_key(prop: dict[str, Any]) -> str:
    return str(prop.get("lofty_property_id") or prop.get("property_name") or prop.get("full_address") or "").strip()


def run_patch_builder(
    python_bin: str,
    patch_script: Path,
    runtime_map: Path,
    prop: dict[str, Any],
    run_month: str | None = None,
) -> tuple[list[str], subprocess.CompletedProcess[str], dict[str, Any]]:
    key = property_key(prop)
    cmd = [
        python_bin,
        str(patch_script),
        "--property",
        key,
        "--property-map",
        str(runtime_map),
        "--financials-only",
    ]
    env = os.environ.copy()
    if run_month:
        env["RUN_MONTH"] = run_month
        # The canonical FINANCIALS.md is the only listing source after its
        # matching review candidate has a hash-bound manifest approval.
        env.pop("LOFTY_PM_APPROVED_FINANCIALS_RUN_MONTH", None)
        env.pop("LOFTY_PM_PREFER_APPROVED_FINANCIALS", None)
    completed = subprocess.run(cmd, text=True, capture_output=True, timeout=120, env=env)
    parsed = extract_json(completed.stdout) if completed.returncode == 0 else {}
    return cmd, completed, parsed


def classify_record(
    *,
    prop: dict[str, Any],
    parsed: dict[str, Any],
    completed: subprocess.CompletedProcess[str],
    live_status: dict[str, Any] | None,
) -> tuple[str, list[str]]:
    issues: list[str] = []
    patch = parsed.get("patch") if isinstance(parsed.get("patch"), dict) else {}
    fields = parsed.get("fields") if isinstance(parsed.get("fields"), list) else sorted(patch.keys())
    field_count = int(parsed.get("field_count") or len(patch))
    source_quality_flags = parsed.get("source_quality_flags") if isinstance(parsed.get("source_quality_flags"), list) else []
    if live_financial_capture_skipped(live_status):
        return "skipped_inactive_or_sold", []
    if "ledger_summary_generated_review_required" in source_quality_flags:
        issues.append("FINANCIALS.md is generated from ledger summary and explicitly requires review before investor email/publish")
    elif "ledger_summary_only" in source_quality_flags:
        issues.append("FINANCIALS.md has ledger summary source only; no reviewed Lofty financial fields were parsed")
    if completed.returncode != 0:
        issues.append(f"financial patch builder failed rc={completed.returncode}")
    if parsed.get("status") in {"missing", "unreadable"}:
        issues.append(str(parsed.get("error") or parsed.get("status")))
    if field_count <= 0:
        issues.append("financial patch has no fields")
    unsafe = sorted(key for key in patch.keys() if key in UNSAFE_PATCH_KEYS)
    if unsafe:
        issues.append(f"financial patch contains unsafe non-financial keys: {', '.join(unsafe)}")
    financials_md = Path(str(prop.get("financials_md") or ""))
    if not financials_md.is_file():
        issues.append(f"financials_md missing: {financials_md}")
    if live_status is None:
        issues.append("missing live financial capture record")
    elif not live_financial_capture_ready(live_status) and not live_financial_corrective_distribution_ready(live_status, patch):
        issues.append(f"live financial capture not apply-ready: status={live_status.get('status') or 'unknown'}")
    if completed.returncode != 0 or parsed.get("status") in {"missing", "unreadable"}:
        return "blocked_patch_failed", issues
    if field_count <= 0:
        return "blocked_empty_patch", issues
    if unsafe:
        return "blocked_unsafe_patch", issues
    if not financials_md.is_file():
        return "blocked_missing_financials_md", issues
    if live_status is None or (
        not live_financial_capture_ready(live_status)
        and not live_financial_corrective_distribution_ready(live_status, patch)
    ):
        return "blocked_missing_live_financial_capture", issues
    return "ready_financial_patch", issues


def build_report(
    *,
    runtime_map: Path,
    financial_patch_script: Path,
    live_financial_capture_report: Path | None,
    python_bin: str,
    review_candidate_packet_report: Path | None = DEFAULT_REVIEW_CANDIDATE_PACKET,
    financial_approval_manifest_path: Path | None = DEFAULT_FINANCIAL_APPROVAL_MANIFEST,
    report_path: Path | None = DEFAULT_REPORT,
) -> dict[str, Any]:
    props, issues = runtime_properties(runtime_map)
    live_capture = read_json(live_financial_capture_report) if live_financial_capture_report else {}
    live_statuses, live_issues = live_financial_statuses(live_financial_capture_report)
    excluded_names = configured_excluded_names(live_financial_capture_report)
    issues.extend(live_issues)
    financial_candidates, candidate_issues, candidate_packet_summary = load_financial_candidate_sources(review_candidate_packet_report)
    financial_approval_manifest = load_financial_approval_manifest(financial_approval_manifest_path) if financial_approval_manifest_path else {}
    issues.extend(candidate_issues)
    run_month = (
        infer_run_month(live_capture, [])
        or str(candidate_packet_summary.get("run_month") or "").strip()
        or None
    )
    records: list[dict[str, Any]] = []
    matched_candidate_keys: set[str] = set()
    status_counts: dict[str, int] = {}
    total_field_count = 0
    for prop in props:
        cmd, completed, parsed = run_patch_builder(python_bin, financial_patch_script, runtime_map, prop, run_month)
        patch = parsed.get("patch") if isinstance(parsed.get("patch"), dict) else {}
        fields = parsed.get("fields") if isinstance(parsed.get("fields"), list) else sorted(patch.keys())
        field_count = int(parsed.get("field_count") or len(patch))
        live_status = (
            live_statuses.get(str(prop.get("lofty_property_id") or "").strip())
            or live_statuses.get(str(prop.get("financials_md") or "").strip())
            or live_statuses.get(str(prop.get("property_name") or "").strip().lower())
        )
        if runtime_property_is_excluded(prop, excluded_names):
            live_status = {"status": "excluded_by_monthly_exclusion_guard", "skipped": True}
        record_status, record_issues = classify_record(
            prop=prop,
            parsed=parsed,
            completed=completed,
            live_status=live_status,
        )
        total_field_count += max(field_count, 0)
        status_counts[record_status] = status_counts.get(record_status, 0) + 1
        record = {
            "property_name": prop.get("property_name") or prop.get("full_address"),
            "lofty_property_id": prop.get("lofty_property_id"),
            "financials_md": prop.get("financials_md"),
            "status": record_status,
            "issue_count": len(record_issues),
            "issues": record_issues,
            "patch_builder_return_code": completed.returncode,
            "patch_builder_command": cmd,
            "field_count": field_count,
            "fields": sorted(str(field) for field in fields),
            "sources": parsed.get("sources") if isinstance(parsed.get("sources"), list) else [],
            "source_quality_flags": parsed.get("source_quality_flags") if isinstance(parsed.get("source_quality_flags"), list) else [],
            "patch_digest": stable_digest(patch) if patch else None,
            "_patch_for_distribution_guard": patch,
            "_live_status_for_distribution_guard": live_status or {},
            "live_financial_guard_status": (live_status or {}).get("status"),
            "live_financial_guard_ok": (live_status or {}).get("guard_ok") is True,
            "live_financials_length": (live_status or {}).get("live_financials_length"),
            "live_financial_snapshot_path": (live_status or {}).get("snapshot_path"),
            "stderr_tail": completed.stderr[-1000:],
        }
        records.append(record)
    run_month = run_month or infer_run_month(live_capture, records)
    for record in records:
        record["approved_financials_target"] = str(financial_approval_manifest_path) if financial_approval_manifest_path else None
        candidate = candidate_for_record(financial_candidates, record)
        if candidate and record.get("status") != "skipped_inactive_or_sold":
            matched_candidate_keys.update(
                key
                for key in (
                    lookup_key(record.get("lofty_property_id")),
                    lookup_key(record.get("property_name")),
                    lookup_key(record.get("financials_md")),
                    lookup_key(candidate.get("candidate_financial_approval_target")),
                    lookup_key(monthly_artifact_property_folder(candidate.get("candidate_financial_approval_target"))),
                    lookup_key(candidate.get("candidate_packet_record_property_name")),
                )
                if key
            )
            candidate_source = str(candidate.get("candidate_financial_source") or "").strip()
            record["candidate_financial_source"] = candidate_source
            record["candidate_packet_record_property_name"] = candidate.get("candidate_packet_record_property_name")
            record["candidate_packet_monthly_financial_summary"] = candidate.get("candidate_packet_monthly_financial_summary") or {}
            record["candidate_packet_financial_snapshot"] = candidate.get("candidate_packet_financial_snapshot") or {}
            record["candidate_source_freshness_issues"] = candidate_source_freshness_issues(
                record["candidate_packet_financial_snapshot"]
                if isinstance(record["candidate_packet_financial_snapshot"], dict)
                else {}
            )
            record["candidate_packet_monthly_financial_summary_issues"] = monthly_summary_issues(
                record["candidate_packet_monthly_financial_summary"],
                run_month,
            )
            record["candidate_packet_monthly_financial_summary_field_coverage"] = monthly_summary_field_coverage(
                record["candidate_packet_monthly_financial_summary"],
                run_month,
            )
            candidate_path = Path(candidate_source)
            record["candidate_financial_source_exists"] = candidate_path.is_file()
            record["candidate_financial_sha256"] = sha256_file(candidate_path) if candidate_path.is_file() else ""
            record["candidate_financial_quality_issues"] = candidate_financial_quality_issues(candidate_path)
            if record["candidate_source_freshness_issues"]:
                record["candidate_financial_quality_issues"].extend(record["candidate_source_freshness_issues"])
                record["issues"] = list(record.get("issues") or []) + record["candidate_source_freshness_issues"]
                record["issue_count"] = len(record["issues"])
                if record.get("status") == "ready_financial_patch":
                    record["status"] = "blocked_candidate_source_stale"
            manifest_approval = approved_candidate(
                financial_approval_manifest,
                run_month=str(run_month or ""),
                canonical_financials=Path(str(record.get("financials_md") or "")),
            )
            approval_target = str(record.get("approved_financials_target") or "").strip()
            record["approval_target_exists"] = manifest_approval is not None
            record["approval_target_quality_issues"] = [] if manifest_approval else ["approval_manifest_missing_or_invalid"]
            record["approval_target_candidate_match_issues"] = []
            if manifest_approval is None:
                record["approval_target_candidate_match_issues"] = ["approval_manifest_missing_or_invalid"]
            elif (
                str(manifest_approval.get("candidate_path") or "") != str(candidate_path)
                or str(manifest_approval.get("candidate_sha256") or "") != record["candidate_financial_sha256"]
            ):
                record["approval_target_candidate_match_issues"] = ["approval_manifest_does_not_match_current_financial_candidate"]
            if record["approval_target_candidate_match_issues"]:
                record["issues"] = list(record.get("issues") or []) + record["approval_target_candidate_match_issues"]
                record["issue_count"] = len(record["issues"])
                if record.get("status") in {"ready_financial_patch", "patch_ready_guard_reconcile_required"}:
                    record["status"] = "blocked_approval_target_stale"
            record["approval_target_safe_for_apply"] = bool(
                record["approval_target_exists"]
                and not record["approval_target_quality_issues"]
                and not record["approval_target_candidate_match_issues"]
            )
        elif review_candidate_packet_report is not None and record.get("status") != "skipped_inactive_or_sold":
            record["candidate_packet_monthly_financial_summary_issues"] = ["runtime property missing review candidate packet match"]
    distribution_guard_live_positive_records = []
    for record in records:
        patch_for_guard = record.pop("_patch_for_distribution_guard", {})
        live_status_for_guard = record.pop("_live_status_for_distribution_guard", {})
        if record.get("status") == "skipped_inactive_or_sold":
            continue
        preview = distribution_guard_preview(
            record.get("candidate_packet_monthly_financial_summary")
            if isinstance(record.get("candidate_packet_monthly_financial_summary"), dict)
            else {},
            live_status_for_guard if isinstance(live_status_for_guard, dict) else {},
            patch_for_guard if isinstance(patch_for_guard, dict) else {},
            record.get("financials_md"),
            record.get("property_name"),
        )
        record["distribution_guard_readiness"] = preview
        if preview["issues"]:
            record["issues"] = list(record.get("issues") or []) + preview["issues"]
            record["issue_count"] = len(record["issues"])
            if record.get("status") == "ready_financial_patch":
                record["status"] = "blocked_distribution_guard_not_live_verified"
            distribution_guard_live_positive_records.append(
                {
                    "property_name": record.get("property_name"),
                    "lofty_property_id": record.get("lofty_property_id"),
                    "financials_md": record.get("financials_md"),
                    "cash_source_guard_sources": preview["cash_source_guard_sources"],
                    "zero_distribution_sources": preview["zero_distribution_sources"],
                    "live_cash_flow_actual": preview["live_cash_flow_actual"],
                    "live_coc_actual": preview["live_coc_actual"],
                    "live_projected_rental_yield_actual": preview["live_projected_rental_yield_actual"],
                    "live_is_occupied_actual": preview["live_is_occupied_actual"],
                    "patch_cash_flow": preview["patch_cash_flow"],
                    "patch_coc": preview["patch_coc"],
                    "patch_projected_rental_yield": preview["patch_projected_rental_yield"],
                    "issues": preview["issues"],
                }
            )
    status_counts = {}
    for record in records:
        status = str(record.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    blocked_count = sum(
        count
        for status, count in status_counts.items()
        if status.startswith("blocked_") or status == "patch_ready_guard_reconcile_required"
    )
    ready_count = status_counts.get("ready_financial_patch", 0)
    guard_reconcile_count = status_counts.get("patch_ready_guard_reconcile_required", 0)
    approval_target_stale_count = status_counts.get("blocked_approval_target_stale", 0)
    blocked_empty_patch_count = status_counts.get("blocked_empty_patch", 0)
    blocked_generated_ledger_review_required_count = sum(
        1
        for record in records
        if record.get("status") == "blocked_empty_patch" and record_has_generated_ledger_review_flag(record)
    )
    candidate_packet_records = candidate_packet_summary.get("records") if isinstance(candidate_packet_summary.get("records"), list) else []
    candidate_packet_monthly_summary_issue_count = sum(
        len(monthly_summary_issues(candidate.get("monthly_financial_summary") if isinstance(candidate, dict) else {}, run_month))
        for candidate in candidate_packet_records
        if not candidate_is_excluded(candidate, excluded_names)
    )
    candidate_packet_monthly_summary_missing_required_field_count = sum(
        monthly_summary_field_coverage(
            candidate.get("monthly_financial_summary") if isinstance(candidate, dict) else {},
            run_month,
        )["missing_required_field_count"]
        for candidate in candidate_packet_records
        if not candidate_is_excluded(candidate, excluded_names)
    )
    candidate_packet_monthly_summary_coverage_totals = monthly_summary_coverage_totals(
        [
            candidate
            for candidate in candidate_packet_records
            if isinstance(candidate, dict) and not candidate_is_excluded(candidate, excluded_names)
        ],
        run_month,
    )
    candidate_packet_missing_runtime_records = []
    excluded_candidate_packet_records = []
    for candidate in candidate_packet_records:
        if not isinstance(candidate, dict):
            continue
        if candidate_is_excluded(candidate, excluded_names):
            excluded_candidate_packet_records.append(candidate)
            continue
        keys = [key for key in candidate_lookup_keys(candidate) if key]
        if keys and not any(key in matched_candidate_keys for key in keys):
            candidate_packet_missing_runtime_records.append(
                {
                    "property_name": candidate.get("property_name"),
                    "lofty_property_id": candidate.get("lofty_property_id"),
                    "financials_md": candidate.get("financials_md"),
                    "financial_candidate": candidate.get("financial_candidate"),
                    "financial_approval_target": candidate.get("financial_approval_target"),
                    "monthly_financial_summary_issues": monthly_summary_issues(
                        candidate.get("monthly_financial_summary") if isinstance(candidate.get("monthly_financial_summary"), dict) else {},
                        run_month,
                    ),
                }
            )
    record_monthly_summary_issue_count = sum(
        len(record.get("candidate_packet_monthly_financial_summary_issues") or [])
        for record in records
        if isinstance(record.get("candidate_packet_monthly_financial_summary_issues"), list)
        and record.get("status") != "skipped_inactive_or_sold"
    )
    runtime_monthly_summary_missing_required_field_count = sum(
        int((record.get("candidate_packet_monthly_financial_summary_field_coverage") or {}).get("missing_required_field_count") or 0)
        for record in records
        if isinstance(record.get("candidate_packet_monthly_financial_summary_field_coverage"), dict)
        and record.get("status") != "skipped_inactive_or_sold"
    )
    runtime_monthly_summary_coverage_totals = monthly_summary_coverage_totals(
        [
            {"monthly_financial_summary": record.get("candidate_packet_monthly_financial_summary")}
            for record in records
            if isinstance(record.get("candidate_packet_monthly_financial_summary"), dict)
            and record.get("status") != "skipped_inactive_or_sold"
        ],
        run_month,
    )
    missing_lofty_reserve_count = int(candidate_packet_summary.get("missing_lofty_reserve_count") or 0)
    missing_lofty_reserve_csv_row_count = csv_row_count(candidate_packet_summary.get("missing_lofty_reserve_csv"))
    missing_lofty_reserve_csv_row_count_ok = (
        missing_lofty_reserve_count == 0
        if missing_lofty_reserve_csv_row_count is None
        else missing_lofty_reserve_csv_row_count == missing_lofty_reserve_count
    )
    candidate_packet_monthly_summary_issue_records = [
        monthly_summary_issue_record(
            source="candidate_packet",
            property_name=candidate.get("property_name"),
            lofty_property_id=candidate.get("lofty_property_id"),
            financials_md=candidate.get("financials_md"),
            financial_candidate=candidate.get("financial_candidate"),
            financial_approval_target=candidate.get("financial_approval_target"),
            summary=candidate.get("monthly_financial_summary") if isinstance(candidate.get("monthly_financial_summary"), dict) else {},
            run_month=run_month,
            reserve_review_csv=candidate_packet_summary.get("missing_lofty_reserve_csv"),
            reserve_review_markdown=candidate_packet_summary.get("missing_lofty_reserve_markdown"),
        )
        for candidate in candidate_packet_records
        if isinstance(candidate, dict)
        and not candidate_is_excluded(candidate, excluded_names)
        and monthly_summary_issues(
            candidate.get("monthly_financial_summary") if isinstance(candidate.get("monthly_financial_summary"), dict) else {},
            run_month,
        )
    ]
    runtime_monthly_summary_issue_records = [
        monthly_summary_issue_record(
            source="runtime",
            property_name=record.get("property_name"),
            lofty_property_id=record.get("lofty_property_id"),
            financials_md=record.get("financials_md"),
            financial_candidate=record.get("candidate_financial_source"),
            financial_approval_target=record.get("approved_financials_target"),
            summary=record.get("candidate_packet_monthly_financial_summary")
            if isinstance(record.get("candidate_packet_monthly_financial_summary"), dict)
            else {},
            run_month=run_month,
            reserve_review_csv=candidate_packet_summary.get("missing_lofty_reserve_csv"),
            reserve_review_markdown=candidate_packet_summary.get("missing_lofty_reserve_markdown"),
        )
        for record in records
        if record.get("status") != "skipped_inactive_or_sold"
        and isinstance(record.get("candidate_packet_monthly_financial_summary_issues"), list)
        and record.get("candidate_packet_monthly_financial_summary_issues")
    ]
    runtime_missing_candidate_count = (
        sum(
            1
            for record in records
            if record.get("status") != "skipped_inactive_or_sold"
            and not str(record.get("candidate_financial_source") or "").strip()
        )
        if review_candidate_packet_report is not None
        else 0
    )
    blocked_empty_patch_candidate_source_count = sum(
        1
        for record in records
        if record.get("status") == "blocked_empty_patch" and str(record.get("candidate_financial_source") or "").strip()
    )
    blocked_empty_patch_candidate_quality_issue_count = sum(
        len(record.get("candidate_financial_quality_issues") or [])
        for record in records
        if record.get("status") == "blocked_empty_patch" and isinstance(record.get("candidate_financial_quality_issues"), list)
    )
    candidate_source_freshness_issue_count = sum(
        len(record.get("candidate_source_freshness_issues") or [])
        for record in records
        if record.get("status") != "skipped_inactive_or_sold"
    )
    guard_reconcile_field_count = sum(
        int(record.get("field_count") or 0)
        for record in records
        if record.get("status") == "patch_ready_guard_reconcile_required"
    )
    readiness_digest = financial_patch_readiness_digest(records)
    artifact_paths = report_artifact_paths(report_path) if report_path else {}
    if not financial_patch_script.is_file():
        issues.append(f"financial patch script missing: {financial_patch_script}")
    if not props:
        issues.append("runtime map has no properties")
    if candidate_packet_monthly_summary_issue_count:
        issues.append(f"review candidate packet has {candidate_packet_monthly_summary_issue_count} monthly financial summary issue(s)")
    if candidate_packet_missing_runtime_records:
        issues.append(f"review candidate packet has {len(candidate_packet_missing_runtime_records)} financial candidate(s) not covered by Lofty runtime patch readiness")
    if runtime_missing_candidate_count:
        issues.append(f"Lofty runtime patch readiness has {runtime_missing_candidate_count} property/properties missing review candidate packet coverage")
    if record_monthly_summary_issue_count:
        issues.append(f"Lofty runtime patch readiness has {record_monthly_summary_issue_count} runtime monthly financial summary issue(s)")
    if candidate_source_freshness_issue_count:
        issues.append(
            f"Lofty runtime patch readiness has {candidate_source_freshness_issue_count} stale or unproven candidate ledger source issue(s)"
        )
    if not missing_lofty_reserve_csv_row_count_ok:
        issues.append(
            "missing Lofty reserve review queue row count mismatch: "
            f"{missing_lofty_reserve_csv_row_count}/{missing_lofty_reserve_count}"
        )
    if distribution_guard_live_positive_records:
        issues.append(
            f"distribution guard has {len(distribution_guard_live_positive_records)} property/properties with positive live or patch distributions while zero-distribution guard is active"
        )
    if blocked_count:
        issues.append(f"financial patch readiness has {blocked_count} blocked/reconcile-required properties")
    if approval_target_stale_count:
        next_action = (
            f"Review and explicitly approve {approval_target_stale_count} current FINANCIALS candidate(s) into the "
            "hash-bound approval manifest before any Lofty PM financial apply."
        )
    elif candidate_packet_missing_runtime_records:
        next_action = (
            f"Review {len(candidate_packet_missing_runtime_records)} review-candidate financial record(s) not covered by "
            "the Lofty runtime map before any live listing financial publish."
        )
    elif candidate_source_freshness_issue_count:
        next_action = (
            "Regenerate every stale or legacy review candidate from its current ECO source ledger, then record a digest-bound "
            "approval in the financial approval manifest before any Lofty PM financial apply."
        )
    elif candidate_packet_monthly_summary_issue_count or record_monthly_summary_issue_count:
        next_action = (
            "Regenerate or repair monthly review candidates so every Lofty financial patch has run-month ECO GL Column E "
            "and Lofty curr_maintenance_reserve evidence before publish."
        )
    elif distribution_guard_live_positive_records:
        next_action = (
            f"Apply and verify zero-distribution guarded live financial patches for {len(distribution_guard_live_positive_records)} "
            "properties before any Lofty PM publish/email."
        )
    elif guard_reconcile_count:
        next_action = (
            f"Review {artifact_paths.get('guard_reconcile_csv')}; {guard_reconcile_count} properties have safe non-empty "
            "financial patches but require live FINANCIALS.md guard reconciliation before any Lofty PM apply."
        )
    elif blocked_empty_patch_count:
        if blocked_generated_ledger_review_required_count == blocked_empty_patch_count:
            next_action = (
                f"Review {artifact_paths.get('blocked_empty_patch_markdown') or artifact_paths.get('blocked_empty_patch_csv')}; "
                f"{blocked_empty_patch_count} properties have generated "
                "ledger-summary FINANCIALS.md files that require a reviewed monthly financial snapshot before any Lofty PM apply."
            )
        else:
            next_action = (
                f"Review {artifact_paths.get('blocked_empty_patch_csv')}; {blocked_empty_patch_count} properties produced empty "
                "financial patches and should not be applied."
            )
    else:
        next_action = "No financial patch readiness action required."
    return {
        "generated_at": iso_z(),
        "status": "ok" if not issues else "review",
        "classification": "lofty-financial-patch-readiness" if not issues else "lofty-financial-patch-readiness-review",
        "issue_count": len(issues),
        "issues": issues,
        "next_action": next_action,
        "mutates_lofty_listing": False,
        "sends_owner_email": False,
        "runtime_map": str(runtime_map),
        "runtime_map_exists": runtime_map.is_file(),
        "financial_patch_script": str(financial_patch_script),
        "financial_patch_script_exists": financial_patch_script.is_file(),
        "live_financial_capture_report": str(live_financial_capture_report) if live_financial_capture_report else None,
        "review_candidate_packet_report": str(review_candidate_packet_report) if review_candidate_packet_report else None,
        "review_candidate_packet_summary": candidate_packet_summary,
        "run_month": run_month,
        "property_count": len(props),
        "candidate_packet_property_count": candidate_packet_summary.get("property_count"),
        "candidate_packet_financial_candidate_count": candidate_packet_summary.get("candidate_count"),
        "missing_lofty_reserve_count": missing_lofty_reserve_count,
        "missing_lofty_reserve_csv": candidate_packet_summary.get("missing_lofty_reserve_csv"),
        "missing_lofty_reserve_markdown": candidate_packet_summary.get("missing_lofty_reserve_markdown"),
        "missing_lofty_reserve_csv_row_count": missing_lofty_reserve_csv_row_count,
        "missing_lofty_reserve_csv_row_count_ok": missing_lofty_reserve_csv_row_count_ok,
        "candidate_packet_monthly_summary_issue_count": candidate_packet_monthly_summary_issue_count,
        "candidate_packet_monthly_summary_missing_required_field_count": candidate_packet_monthly_summary_missing_required_field_count,
        "candidate_packet_monthly_summary_coverage_totals": candidate_packet_monthly_summary_coverage_totals,
        "candidate_packet_monthly_summary_issue_record_count": len(candidate_packet_monthly_summary_issue_records),
        "candidate_packet_monthly_summary_issue_records": candidate_packet_monthly_summary_issue_records[:100],
        "candidate_packet_missing_runtime_count": len(candidate_packet_missing_runtime_records),
        "candidate_packet_missing_runtime_records": candidate_packet_missing_runtime_records,
        "candidate_packet_excluded_count": len(excluded_candidate_packet_records),
        "candidate_packet_excluded_records": excluded_candidate_packet_records,
        "excluded_property_names": excluded_names,
        "runtime_missing_candidate_count": runtime_missing_candidate_count,
        "runtime_monthly_summary_issue_count": record_monthly_summary_issue_count,
        "runtime_monthly_summary_missing_required_field_count": runtime_monthly_summary_missing_required_field_count,
        "runtime_monthly_summary_coverage_totals": runtime_monthly_summary_coverage_totals,
        "runtime_monthly_summary_issue_record_count": len(runtime_monthly_summary_issue_records),
        "runtime_monthly_summary_issue_records": runtime_monthly_summary_issue_records[:100],
        "distribution_guard_live_positive_count": len(distribution_guard_live_positive_records),
        "distribution_guard_live_positive_records": distribution_guard_live_positive_records[:100],
        "ready_financial_patch_count": ready_count,
        "approval_target_stale_count": approval_target_stale_count,
        "guard_reconcile_required_count": guard_reconcile_count,
        "guard_reconcile_required_field_count": guard_reconcile_field_count,
        "blocked_empty_patch_count": blocked_empty_patch_count,
        "blocked_empty_patch_candidate_source_count": blocked_empty_patch_candidate_source_count,
        "blocked_empty_patch_candidate_quality_issue_count": blocked_empty_patch_candidate_quality_issue_count,
        "candidate_source_freshness_issue_count": candidate_source_freshness_issue_count,
        "blocked_generated_ledger_review_required_count": blocked_generated_ledger_review_required_count,
        "blocked_count": blocked_count,
        "field_count_total": total_field_count,
        "financial_patch_readiness_digest": readiness_digest,
        "guard_reconcile_csv": str(artifact_paths.get("guard_reconcile_csv")) if artifact_paths else None,
        "blocked_empty_patch_csv": str(artifact_paths.get("blocked_empty_patch_csv")) if artifact_paths else None,
        "blocked_empty_patch_markdown": str(artifact_paths.get("blocked_empty_patch_markdown")) if artifact_paths else None,
        "record_status_counts": dict(sorted(status_counts.items())),
        "records": records,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a non-mutating readiness report for Lofty PM financial listing patches")
    parser.add_argument("--runtime-map", type=Path, default=DEFAULT_RUNTIME_MAP)
    parser.add_argument("--financial-patch-script", type=Path, default=DEFAULT_PATCH_SCRIPT)
    parser.add_argument("--live-financial-capture-report", type=Path, default=DEFAULT_LIVE_FINANCIAL_CAPTURE)
    parser.add_argument("--review-candidate-packet-report", type=Path, default=DEFAULT_REVIEW_CANDIDATE_PACKET)
    parser.add_argument("--financial-approval-manifest", type=Path, default=DEFAULT_FINANCIAL_APPROVAL_MANIFEST)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--python-bin", default=sys.executable or "python3")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_report(
        runtime_map=args.runtime_map,
        financial_patch_script=args.financial_patch_script,
        live_financial_capture_report=args.live_financial_capture_report,
        review_candidate_packet_report=args.review_candidate_packet_report,
        financial_approval_manifest_path=args.financial_approval_manifest,
        python_bin=args.python_bin,
        report_path=args.report,
    )
    write_review_artifacts(report, args.report)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "issue_count": report["issue_count"],
                "property_count": report["property_count"],
                "ready_financial_patch_count": report["ready_financial_patch_count"],
                "guard_reconcile_required_count": report["guard_reconcile_required_count"],
                "blocked_count": report["blocked_count"],
            },
            indent=2,
        )
    )
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
