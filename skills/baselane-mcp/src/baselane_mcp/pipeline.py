"""Bounded CPAI pipeline operations exposed through the Baselane MCP.

This module deliberately exposes an allowlist of canonical workflows and
artifacts.  It is not a general subprocess or filesystem interface.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import date
from pathlib import Path
from typing import Any


MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
MAX_ARTIFACT_BYTES = 5_000_000

ARTIFACT_PATHS = {
    "dao_cash_reconciliation": "reports/baselane_live_dao_cash_reconciliation.json",
    "monthly_run": "reports/baselane_financials_monthly_run_report.json",
    "monthly_readiness": "reports/baselane_financials_monthly_readiness.json",
    "review_candidate_packet": "reports/baselane_financials_monthly_review_candidate_packet.json",
    "discord_all_send_plan": "reports/baselane_financials_monthly_discord_all_send_plan.json",
    "discord_all_send_validation": "reports/baselane_financials_monthly_discord_all_send_plan_validation.json",
}


class PipelineValidationError(ValueError):
    """Raised when a caller requests anything outside the bounded contract."""


def validate_iso_date(value: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise PipelineValidationError("expected an ISO date in YYYY-MM-DD form") from exc


def validate_run_month(value: str) -> str:
    value = str(value or "")
    if not MONTH_RE.fullmatch(value):
        raise PipelineValidationError("expected a run month in YYYY-MM form")
    return value


def _base_env(workspace_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["OPENCLAW_WORKSPACE_ROOT"] = str(workspace_root)
    env["WORKSPACE_ROOT"] = str(workspace_root)
    return env


def _run(
    command: list[str],
    *,
    workspace_root: Path,
    timeout: int,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    try:
        result = subprocess.run(
            command,
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env or _base_env(workspace_root),
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "returncode": None,
            "command_name": Path(command[0]).name,
            "error": f"bounded workflow exceeded {timeout} seconds",
        }
    return {
        "status": "success" if result.returncode == 0 else "review_required",
        "returncode": result.returncode,
        "command_name": Path(command[0]).name,
        "stdout_tail": (result.stdout or "")[-4000:],
        "stderr_tail": (result.stderr or "")[-4000:],
    }


def validate_intercompany_policy(
    *, workspace_root: Path, as_of: str
) -> dict[str, Any]:
    """Validate exact override rules against the ID-bearing source index."""
    cutoff = validate_iso_date(as_of)
    result = _run(
        [
            "python3",
            str(workspace_root / "scripts" / "baselane_validate_intercompany_policy.py"),
            "--as-of",
            cutoff,
        ],
        workspace_root=workspace_root,
        timeout=120,
    )
    if result["returncode"] is not None:
        try:
            result["result"] = json.loads(result["stdout_tail"])
        except json.JSONDecodeError:
            pass
    result["mode"] = "read_only_local_evidence"
    result["as_of"] = cutoff
    return result


def rebuild_dao_cash_reconciliation(
    *, workspace_root: Path, as_of: str
) -> dict[str, Any]:
    """Rebuild the canonical live, read-only DAO cash artifacts."""
    cutoff = validate_iso_date(as_of)
    result = _run(
        [
            "python3",
            str(workspace_root / "scripts" / "baselane_live_dao_cash_reconciliation.py"),
            "--as-of",
            cutoff,
        ],
        workspace_root=workspace_root,
        timeout=420,
    )
    result.update(
        {
            "mode": "live_read_only",
            "as_of": cutoff,
            "artifacts": {
                "json": str(workspace_root / ARTIFACT_PATHS["dao_cash_reconciliation"]),
                "csv": str(
                    workspace_root
                    / "reports"
                    / "baselane_live_dao_cash_reconciliation.csv"
                ),
            },
        }
    )
    return result


def rebuild_monthly_review_artifacts(
    *, workspace_root: Path, run_month: str, reporting_cutoff_date: str
) -> dict[str, Any]:
    """Run the canonical monthly close with every live/send switch forced off."""
    month = validate_run_month(run_month)
    cutoff = validate_iso_date(reporting_cutoff_date)
    env = _base_env(workspace_root)
    env.update(
        {
            "RUN_MONTH": month,
            "REPORTING_CUTOFF_DATE": cutoff,
            "DRY_RUN": "1",
            "BASELANE_MONTHLY_LIVE_ACTIONS_APPROVED": "0",
            "APPLY_BASELANE_MONTHLY_ACCRUALS_LIVE": "0",
            "SWEEP_MONTHLY_DAO_INTEREST_TO_ECO": "0",
            "APPLY_MONTHLY_DAO_INTEREST_SWEEP_LIVE": "0",
            "SETTLE_MONTHLY_RESERVE_RETENTION_CASH": "0",
            "APPLY_LOFTY_GUARDED_UPDATES": "0",
            "APPLY_LOFTY_LIVE_FINANCIAL_CORRECTIONS": "0",
            "RUN_LOFTY_GUARDED_APPLY": "0",
            "BOOTSTRAP_LOFTY_PUBLIC_DOCS": "0",
            "RUN_FUTURE_CF_VALUES_CLEANUP": "0",
            "RUN_DAO_VENDOR_UPSTREAM_NORMALIZATION": "0",
            "RUN_NONPROPERTY_CATEGORY_NORMALIZATION": "0",
            "AUTO_APPROVE_SAFE_REVIEW_CANDIDATES": "0",
            "PUBLISH_LOFTY_PM_UPDATES": "0",
            "SEND_OWNER_EMAILS": "0",
            "SEND_NATIVE_LOFTY_OWNER_EMAILS": "0",
            "SEND_NON_NATIVE_OWNER_EMAILS": "0",
            "SEND_TRANSFER_RECONCILIATION_TELEGRAM": "0",
            "SEND_MONTHLY_DISCORD_PROPERTY_UPDATE": "0",
            "SEND_MONTHLY_DISCORD_REVIEW_DRAFTS": "0",
            "YHOME_GSHEET_APPLY": "0",
        }
    )
    result = _run(
        ["bash", str(workspace_root / "scripts" / "baselane_financials_monthly_cron.sh")],
        workspace_root=workspace_root,
        timeout=7200,
        env=env,
    )
    result.update(
        {
            "mode": "forced_dry_run_no_external_writes",
            "run_month": month,
            "reporting_cutoff_date": cutoff,
            "monthly_run_artifact": str(
                workspace_root / ARTIFACT_PATHS["monthly_run"]
            ),
        }
    )
    return result


def _property_matches(value: Any, property_name: str) -> bool:
    return property_name.casefold() in str(value or "").casefold()


def inspect_pipeline_artifact(
    *,
    workspace_root: Path,
    artifact: str,
    property_name: str | None = None,
    include_payload: bool = False,
) -> dict[str, Any]:
    """Inspect one allowlisted JSON artifact without exposing arbitrary files."""
    if artifact not in ARTIFACT_PATHS:
        raise PipelineValidationError(
            "unsupported artifact; choose one of: " + ", ".join(sorted(ARTIFACT_PATHS))
        )
    path = workspace_root / ARTIFACT_PATHS[artifact]
    if not path.is_file():
        return {"status": "missing", "artifact": artifact, "path": str(path)}
    size = path.stat().st_size
    if size > MAX_ARTIFACT_BYTES:
        return {
            "status": "too_large",
            "artifact": artifact,
            "path": str(path),
            "size_bytes": size,
            "maximum_bytes": MAX_ARTIFACT_BYTES,
        }
    raw = path.read_bytes()
    payload = json.loads(raw)
    result: dict[str, Any] = {
        "status": "success",
        "artifact": artifact,
        "path": str(path),
        "size_bytes": size,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "artifact_status": payload.get("status") if isinstance(payload, dict) else None,
    }
    if property_name:
        matches: list[dict[str, Any]] = []
        if isinstance(payload, dict):
            for key in (
                "properties",
                "records",
                "candidates",
                "reporting_targets",
                "intercompany_subledger",
            ):
                values = payload.get(key)
                if not isinstance(values, list):
                    continue
                for row in values:
                    if not isinstance(row, dict):
                        continue
                    names = (
                        row.get("property"),
                        row.get("property_name"),
                        row.get("address"),
                        row.get("canonical_property"),
                    )
                    if any(_property_matches(value, property_name) for value in names):
                        matches.append({"section": key, "record": row})
        result["property_query"] = property_name
        result["match_count"] = len(matches)
        result["matches"] = matches
    elif include_payload:
        result["payload"] = payload
    elif isinstance(payload, dict):
        result["top_level_fields"] = sorted(payload)
        result["counts"] = {
            key: len(value)
            for key, value in payload.items()
            if isinstance(value, list)
        }
        for key in (
            "generated_at",
            "as_of",
            "run_month",
            "reporting_cutoff_date",
            "issue_count",
            "property_count",
        ):
            if key in payload:
                result[key] = payload[key]
    return result
