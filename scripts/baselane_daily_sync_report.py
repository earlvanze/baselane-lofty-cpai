#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EXPECTED_LOCAL_MODEL = "ollama-cyber/qwen3.5:35b-a3b"
EXPECTED_LOCAL_PROVIDER = "ollama-cyber"
EXPECTED_LOCAL_MODEL_ID = "qwen3.5:35b-a3b"
LOCAL_MODEL_PREFLIGHT_MAX_AGE_HOURS = 30.0
REQUIRE_LOCAL_MODEL_PREFLIGHT = os.environ.get("BASELANE_REQUIRE_LOCAL_MODEL_PREFLIGHT_FOR_DAILY_SYNC", "0") == "1"
DAILY_SOURCE_CASH_BALANCE_MAX_AGE_HOURS = 36.0
BASELANE_LOGIN_REQUIRED_REASONS = {
    "baselane_login_required",
    "baselane_login_wait_failed",
    "cdp_login_failed",
    "recovery_attempted_but_baselane_not_verified",
}


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


def monthly_statements_expected_wait(report: dict[str, Any]) -> bool:
    status = str(report.get("status") or "").strip()
    action = str(report.get("action") or "").strip()
    reason = str(report.get("reason") or "").strip()
    error_class = str(report.get("download_error_class") or "").strip()
    download_error = str(report.get("download_error") or report.get("error") or "")
    no_buttons = (
        reason == "no-statement-buttons"
        or error_class == "no-statement-buttons"
        or "no statement download buttons discovered" in download_error
    )
    return status == "review" and action == "wait-for-statements" and no_buttons


def read_jsonl_tail(path: Path, max_lines: int = 200) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-max_lines:]
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def count(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def normalize_property_name(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def property_tokens(value: object) -> set[str]:
    aliases = {"s": "south", "n": "north", "e": "east", "w": "west", "st": "street"}
    return {
        aliases.get(token, token)
        for token in normalize_property_name(value).split()
        if token not in {"public"}
    }


def token_subset_match(left: object, right: object) -> bool:
    left_tokens = property_tokens(left)
    right_tokens = property_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    return left_tokens <= right_tokens or right_tokens <= left_tokens


def sha256ish(value: object) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", str(value or "")))


def file_sha256(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            return digest.hexdigest()
    except OSError:
        return None


def canonical_extra_rows_are_accrual_overlay(canonical_path: Path, snapshot_path: Path) -> tuple[bool, dict[str, Any]]:
    try:
        canonical_lines = canonical_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        snapshot_lines = snapshot_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError as exc:
        return False, {"status": "unreadable", "error": str(exc)}
    if len(canonical_lines) <= len(snapshot_lines):
        return False, {"status": "not_append_only", "canonical_line_count": len(canonical_lines), "snapshot_line_count": len(snapshot_lines)}
    if canonical_lines[: len(snapshot_lines)] != snapshot_lines:
        return False, {"status": "prefix_mismatch", "canonical_line_count": len(canonical_lines), "snapshot_line_count": len(snapshot_lines)}
    extra_rows = canonical_lines[len(snapshot_lines) :]
    bad_rows = []
    for row in extra_rows:
        is_overlay_row = "ECO Systems Accrual Overlay" in row and "AOPS-" in row
        is_manual_accrual = "ACCRUAL|" in row and "no bank transfer" in row
        is_pm_fee_accrual = "PM-FEE|" in row and "PM fee accrual" in row
        if not is_overlay_row or not (is_manual_accrual or is_pm_fee_accrual):
            bad_rows.append(row)
    return not bad_rows, {
        "status": "ok_accrual_overlay_append_only" if not bad_rows else "non_accrual_extra_rows",
        "extra_row_count": len(extra_rows),
        "bad_row_count": len(bad_rows),
        "bad_rows_bounded": bad_rows[:5],
    }


def normalized_path_text(path_value: object) -> str | None:
    text = str(path_value or "").strip()
    if not text:
        return None
    try:
        return str(Path(text).expanduser().resolve(strict=False))
    except OSError:
        return text


def stat_mtime_iso(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")
    except OSError:
        return None


def run_git_status(repo_dir: Path, paths: list[str]) -> tuple[str | None, str | None]:
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={repo_dir}", "-C", str(repo_dir), "status", "--porcelain", "--", *paths],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        return None, f"git_status_error:{type(exc).__name__}"
    if result.returncode != 0:
        return None, f"git_status_rc_{result.returncode}"
    return result.stdout.strip(), None


def run_git_output(repo_dir: Path, args: list[str]) -> tuple[str | None, str | None]:
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={repo_dir}", "-C", str(repo_dir), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        return None, f"git_error:{type(exc).__name__}"
    if result.returncode != 0:
        return None, f"git_rc_{result.returncode}"
    return result.stdout.strip(), None


def parse_ahead_behind(value: str | None) -> tuple[int | None, int | None]:
    parts = str(value or "").split()
    if len(parts) != 2:
        return None, None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None, None


def assetrail_live_state(assetrail_push: dict[str, Any]) -> dict[str, Any]:
    ledger_path_raw = str(assetrail_push.get("ledger_path") or "").strip()
    ledger_dir_raw = str(assetrail_push.get("ledger_dir") or "").strip()
    if not ledger_path_raw:
        return {"status": "unchecked", "reason": "missing_ledger_path"}
    ledger_path = Path(ledger_path_raw)
    ledger_dir = Path(ledger_dir_raw) if ledger_dir_raw else ledger_path.parent
    state: dict[str, Any] = {
        "status": "ok",
        "ledger_path": str(ledger_path),
        "ledger_dir": str(ledger_dir),
        "ledger_exists": ledger_path.exists(),
        "ledger_size_bytes": None,
        "ledger_mtime": None,
        "ledger_git_status": None,
        "git_head": None,
        "git_upstream": None,
        "git_upstream_head": None,
        "git_upstream_ahead_count": None,
        "git_upstream_behind_count": None,
        "temp_ledger_git_status_count": 0,
        "temp_ledger_git_statuses_bounded": [],
        "issues": [],
    }
    if not ledger_path.exists():
        state["status"] = "review"
        state["issues"].append("assetrail_live_ledger_missing")
        return state
    try:
        state["ledger_size_bytes"] = ledger_path.stat().st_size
    except OSError:
        state["issues"].append("assetrail_live_ledger_stat_failed")
    state["ledger_mtime"] = stat_mtime_iso(ledger_path)
    expected_size = assetrail_push.get("ledger_size_bytes")
    if expected_size is not None and state["ledger_size_bytes"] is not None and count(expected_size) != state["ledger_size_bytes"]:
        state["issues"].append("assetrail_push_report_stale_for_live_ledger")
    if (ledger_dir / ".git").exists():
        ledger_status, ledger_error = run_git_status(ledger_dir, [ledger_path.name])
        if ledger_error:
            state["issues"].append(f"assetrail_live_ledger_git_status={ledger_error}")
        else:
            state["ledger_git_status"] = ledger_status or ""
            if ledger_status:
                state["issues"].append(f"assetrail_live_ledger_git_dirty={ledger_status}")
        temp_status, temp_error = run_git_status(ledger_dir, ["ECO Systems General Ledger.tmp*.csv"])
        if temp_error:
            state["issues"].append(f"assetrail_temp_ledger_git_status={temp_error}")
        elif temp_status:
            statuses = [line for line in temp_status.splitlines() if line.strip()]
            state["temp_ledger_git_status_count"] = len(statuses)
            state["temp_ledger_git_statuses_bounded"] = statuses[:10]
            state["issues"].append(f"assetrail_temp_ledgers_present={len(statuses)}")
        git_head, git_head_error = run_git_output(ledger_dir, ["rev-parse", "HEAD"])
        git_upstream, git_upstream_error = run_git_output(
            ledger_dir, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]
        )
        git_upstream_head, git_upstream_head_error = run_git_output(ledger_dir, ["rev-parse", "@{u}"])
        ahead_behind, ahead_behind_error = run_git_output(
            ledger_dir, ["rev-list", "--left-right", "--count", "HEAD...@{u}"]
        )
        state["git_head"] = git_head
        state["git_upstream"] = git_upstream
        state["git_upstream_head"] = git_upstream_head
        ahead_count, behind_count = parse_ahead_behind(ahead_behind)
        state["git_upstream_ahead_count"] = ahead_count
        state["git_upstream_behind_count"] = behind_count
        if git_head_error:
            state["issues"].append(f"assetrail_live_git_head={git_head_error}")
        if git_upstream_error:
            state["issues"].append(f"assetrail_live_git_upstream={git_upstream_error}")
        if git_upstream_head_error:
            state["issues"].append(f"assetrail_live_git_upstream_head={git_upstream_head_error}")
        if ahead_behind_error:
            state["issues"].append(f"assetrail_live_git_upstream_status={ahead_behind_error}")
        if git_head and git_upstream_head and git_head != git_upstream_head:
            state["issues"].append("assetrail_upstream_not_current")
        if ahead_count or behind_count:
            state["issues"].append(f"assetrail_upstream_ahead_behind={ahead_count or 0}/{behind_count or 0}")
    if state["issues"]:
        state["status"] = "review"
    return state


def iso_age_hours(value: object) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return round((datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() / 3600, 3)


def iso_epoch_seconds(value: object) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).timestamp()


def fresh_generated_at(report: dict[str, Any], max_age_hours: float = LOCAL_MODEL_PREFLIGHT_MAX_AGE_HOURS) -> bool:
    if not str(report.get("generated_at") or "").strip():
        return False
    age_hours = iso_age_hours(report.get("generated_at"))
    return age_hours is not None and -1 <= age_hours <= max_age_hours


def report_age_hours(report: dict[str, Any], path: Path) -> float | None:
    if str(report.get("generated_at") or "").strip():
        return iso_age_hours(report.get("generated_at"))
    modified = mtime(path)
    if modified > 0:
        return epoch_age_hours(modified)
    return None


def fresh_report(report: dict[str, Any], path: Path, max_age_hours: float) -> bool:
    age_hours = report_age_hours(report, path)
    return age_hours is not None and -1 <= age_hours <= max_age_hours


def scheduler_job(data: dict[str, Any], name: str) -> dict[str, Any]:
    for job in data.get("jobs") or []:
        if isinstance(job, dict) and job.get("name") == name:
            return job
    return {}


def local_model_ok(report: dict[str, Any]) -> bool:
    direct = report.get("direct_smoke") if isinstance(report.get("direct_smoke"), dict) else {}
    finance = report.get("finance_contract_smoke") if isinstance(report.get("finance_contract_smoke"), dict) else {}
    contract = report.get("validation_contract") if isinstance(report.get("validation_contract"), dict) else {}
    scope = report.get("model_execution_scope") if isinstance(report.get("model_execution_scope"), dict) else {}
    contract_scope = contract.get("model_execution_scope") if isinstance(contract.get("model_execution_scope"), dict) else {}
    return (
        report.get("status") == "ok"
        and report.get("model") == EXPECTED_LOCAL_MODEL
        and report.get("provider") == EXPECTED_LOCAL_PROVIDER
        and report.get("model_id") == EXPECTED_LOCAL_MODEL_ID
        and count(report.get("issue_count")) == 0
        and report.get("configured_model_present") is True
        and report.get("selected_endpoint_from_config") is True
        and report.get("model_available") is True
        and report.get("small_model_execution_allowed") is False
        and report.get("small_model_pipeline_execution_allowed") is False
        and report.get("small_model_task_scoped_execution_allowed") is True
        and scope.get("deterministic_only") is True
        and scope.get("pipeline_execution_allowed") is False
        and "calculating ledger balances" in set(scope.get("forbidden_uses") or [])
        and direct.get("attempted") is True
        and direct.get("ok") is True
        and direct.get("response") == "BASELANE_MODEL_OK"
        and finance.get("attempted") is True
        and finance.get("ok") is True
        and finance.get("response") == report.get("finance_contract_expected_response")
        and contract.get("direct_smoke_ok") is True
        and contract.get("direct_smoke_response") == "BASELANE_MODEL_OK"
        and contract.get("finance_contract_smoke_ok") is True
        and contract.get("finance_contract_response") == report.get("finance_contract_expected_response")
        and contract.get("model_scope_deterministic") is True
        and contract.get("model_pipeline_execution_denied") is True
        and contract_scope.get("deterministic_only") is True
        and contract_scope.get("pipeline_execution_allowed") is False
        and sha256ish(report.get("validation_digest"))
        and fresh_generated_at(report)
    )


def rebase_workspace_artifact_path(root: Path, path_value: str) -> str:
    raw = str(path_value or "").strip()
    marker = "/home/umbrel/.openclaw/workspace/"
    if marker not in raw:
        return raw
    relative = raw.split(marker, 1)[1].lstrip("/")
    if not relative:
        return raw
    return str(root / relative)


def source_index_status(
    root: Path,
    export_guard: dict[str, Any],
    login_export: dict[str, Any] | None = None,
) -> tuple[str, str]:
    login_export = login_export or {}
    source_record = (
        export_guard
        if export_guard.get("source_transaction_index") or not login_export.get("source_transaction_index")
        else login_export
    )
    source_index = rebase_workspace_artifact_path(root, str(source_record.get("source_transaction_index") or "").strip())
    if not source_index:
        return ("missing", "") if source_record.get("status") == "missing" else ("missing_path", "")
    path = Path(source_index)
    if not path.is_absolute():
        path = root / source_index
    if not path.is_file():
        return "missing_file", str(path)
    write_status = str(source_record.get("source_transaction_index_current_write_status") or "").strip()
    if write_status and write_status != "written_current":
        return write_status, str(path)
    required = {"BaselaneId", "ISODate", "PropertyId"}
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            fields = {field.strip() for field in reader.fieldnames or []}
            if not fields:
                return "empty_file", str(path)
            missing = sorted(required - fields)
            if missing:
                return f"missing_columns:{','.join(missing)}", str(path)
            data_rows = [
                row
                for row in reader
                if any(str(value or "").strip() for value in row.values())
            ]
    except OSError as exc:
        return f"unreadable:{exc}", str(path)
    except csv.Error as exc:
        return f"unreadable_csv:{exc}", str(path)
    if not data_rows:
        return "empty_rows", str(path)
    usable_rows = [
        row
        for row in data_rows
        if all(str(row.get(field) or "").strip() for field in required)
    ]
    if not usable_rows:
        return "no_usable_rows", str(path)
    return "ok", str(path)


def mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def epoch_age_hours(value: object) -> float | None:
    try:
        epoch = float(value)
    except (TypeError, ValueError):
        return None
    return round((datetime.now(timezone.utc).timestamp() - epoch) / 3600, 3)


def epoch_seconds(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def recent_deterministic_wrapper_failures(history_path: Path, window_hours: float = 24.0) -> tuple[int, list[dict[str, Any]]]:
    records_by_run: dict[str, dict[str, Any]] = {}
    for record in read_jsonl_tail(history_path):
        ended_at = record.get("ended_at") or record.get("finished_at")
        age_hours = iso_age_hours(ended_at)
        if age_hours is None or age_hours < -1 or age_hours > window_hours:
            continue
        steps = record.get("steps") if isinstance(record.get("steps"), dict) else {}
        deterministic_status = str(steps.get("deterministic_sync") or "").strip()
        failed_step = str(record.get("failed_step") or "").strip()
        status = str(record.get("status") or "").strip()
        deterministic_failed = (
            failed_step == "baselane_sync_cdp_deterministic"
            or deterministic_status.startswith("failed_rc_")
        ) and status in {"failed", "review"}
        if not deterministic_failed:
            continue
        run_key = str(record.get("started_at") or ended_at or len(records_by_run)).strip()
        records_by_run[run_key] = {
            "started_at": record.get("started_at"),
            "ended_at": ended_at,
            "status": status,
            "return_code": record.get("return_code"),
            "failed_step": failed_step or None,
            "deterministic_sync": deterministic_status or None,
        }
    records = list(records_by_run.values())
    records.sort(key=lambda item: str(item.get("ended_at") or ""))
    return len(records), records[-5:]


def build_report(root: Path) -> dict[str, Any]:
    reports = root / "reports"
    daily_path = reports / "baselane_daily_run_report.json"
    daily_history_path = reports / "baselane_daily_run_history.jsonl"
    sync_path = reports / "baselane_sync_cdp_report.json"
    monthly_statements_gate_path = reports / "baselane_monthly_statements_idempotent_report.json"
    daily = read_json(daily_path)
    sync = read_json(sync_path)
    monthly_statements_gate = read_json(monthly_statements_gate_path)
    split = read_json(reports / "split_ledger_public_financials_last.json")
    source_cash_path = reports / "baselane_daily_source_cash_balance_report.json"
    source_cash = read_json(source_cash_path)
    first_day_pm_fee = read_json(reports / "baselane_first_day_pm_fee_audit.json")
    first_day_pm_fee_cleanup = read_json(reports / "baselane_first_day_pm_fee_source_cleanup_plan.json")
    pm_fee_duplicate_lane = read_json(reports / "baselane_pm_fee_duplicate_lane_audit.json")
    assetrail_push = read_json(reports / "baselane_assetrail_push_report.json")
    native_split_overlay = read_json(reports / "baselane_native_split_ledger_overlay_report.json")
    hemlane_live_transactions = read_json(reports / "hemlane_live_transactions.json")
    hemlane_auto_tag = read_json(reports / "baselane_hemlane_auto_tag_report.json")
    model = read_json(reports / "baselane_local_model_preflight_report.json")
    disk_preflight = read_json(reports / "baselane_daily_disk_space_preflight_report.json")
    login_wait = read_json(reports / "baselane_login_wait_report.json")
    cdp_auth_recovery = read_json(reports / "baselane_cdp_auth_recovery_report.json")
    current_auth_recovery = read_json(reports / "baselane_auth_recovery_report.json")
    current_auth_verified = current_auth_recovery.get("status") == "ok"
    scope_guard = read_json(reports / "baselane_daily_scope_guard.json")
    login_export = read_json(reports / "baselane_login_export_report.json")
    export_guard = read_json(reports / "baselane_export_guard_last.json")
    export_guard_path = reports / "baselane_export_guard_last.json"
    export_guard_status = export_guard.get("status")
    if not export_guard_status:
        if export_guard.get("ok") is True:
            export_guard_status = "ok"
        elif export_guard.get("ok") is False:
            export_guard_status = "review"
        else:
            export_guard_status = export_guard.get("status")
    scheduler = read_json(reports / "baselane_scheduler_audit_report.json")
    daily_job = scheduler_job(scheduler, "daily_sync")
    current_root_raw = str(root)
    current_root_text = normalized_path_text(root)
    daily_run_workspace_root = str(daily.get("workspace_root") or "").strip() or None
    daily_run_openclaw_root = str(daily.get("openclaw_root") or "").strip() or None
    daily_run_workspace_root_normalized = normalized_path_text(daily_run_workspace_root)
    daily_run_workspace_root_raw_matches_current = (
        None if not daily_run_workspace_root else daily_run_workspace_root == current_root_raw
    )
    daily_run_workspace_root_matches_current = (
        None
        if not daily_run_workspace_root_normalized or not current_root_text
        else daily_run_workspace_root_normalized == current_root_text
    )
    daily_run_workspace_root_aliases_current = (
        daily_run_workspace_root_raw_matches_current is False
        and daily_run_workspace_root_matches_current is True
    )
    daily_run_foreign_workspace_root = daily_run_workspace_root_matches_current is False
    daily_run_age_hours = iso_age_hours(daily.get("ended_at"))
    export_guard_written_at = stat_mtime_iso(export_guard_path)
    export_guard_after_daily_run = False
    if daily_run_age_hours is not None and daily_run_age_hours <= 36.0 and export_guard_written_at and daily.get("ended_at"):
        try:
            export_guard_after_daily_run = (
                datetime.fromisoformat(export_guard_written_at.replace("Z", "+00:00"))
                > datetime.fromisoformat(str(daily["ended_at"]).replace("Z", "+00:00"))
            )
        except ValueError:
            pass
    if export_guard_after_daily_run:
        export_guard_status = "unrelated_newer_guard"
    steps = daily.get("steps") if isinstance(daily.get("steps"), dict) else {}
    hemlane_step_status = str(steps.get("hemlane_live_transaction_evidence") or "").strip()
    hemlane_live_transaction_status = str(hemlane_live_transactions.get("status") or "").strip()
    hemlane_live_transaction_required = (
        (
            bool(hemlane_step_status)
            and not hemlane_step_status.startswith("skipped_")
            and hemlane_step_status not in {"not_started", "disabled"}
        )
        or hemlane_live_transaction_status not in {"", "missing", "unreadable"}
    )
    hemlane_live_transaction_issue = (
        hemlane_live_transaction_required
        and hemlane_live_transaction_status != "ok"
    )
    hemlane_auto_tag_step_status = str(steps.get("hemlane_auto_tag_source_fix") or "").strip()
    hemlane_auto_tag_status = str(hemlane_auto_tag.get("status") or "").strip()
    hemlane_auto_tag_required = (
        (
            bool(hemlane_auto_tag_step_status)
            and not hemlane_auto_tag_step_status.startswith("skipped_")
            and hemlane_auto_tag_step_status not in {"not_started", "disabled"}
        )
        or hemlane_auto_tag_status not in {"", "missing", "unreadable"}
    )
    hemlane_auto_tag_issue = (
        hemlane_auto_tag_required
        and hemlane_auto_tag_status != "ok"
    )
    human_paced_backup_script = str(
        daily.get("human_paced_backup_script")
        or (root / "scripts" / "baselane_sync_cdp_human_paced.py")
    )
    human_paced_backup_enabled = daily.get("human_paced_backup_enabled")
    if human_paced_backup_enabled is None:
        human_paced_backup_enabled = True
    human_paced_backup_script_exists = daily.get("human_paced_backup_script_exists")
    if human_paced_backup_script_exists is None:
        human_paced_backup_script_exists = True
    human_paced_backup_policy = str(
        daily.get("human_paced_backup_policy") or "deterministic_primary_human_paced_backup"
    )
    session_seed_status = str(steps.get("session_seed") or daily.get("session_seed_status") or "").strip()
    disk_preflight_status = str(
        disk_preflight.get("status") or steps.get("disk_space_preflight") or ""
    ).strip()
    disk_preflight_issues = [
        str(issue)
        for issue in (disk_preflight.get("issues") if isinstance(disk_preflight.get("issues"), list) else [])
        if str(issue).strip()
    ]
    required_steps = ["deterministic_sync"]
    if REQUIRE_LOCAL_MODEL_PREFLIGHT:
        required_steps.append("local_model_preflight")
    missing_steps = [step for step in required_steps if step not in steps]
    scheduler_issues_raw = [str(issue) for issue in daily_job.get("issues") or [] if issue]
    self_reconciled_scheduler_issues: set[str] = set()
    if human_paced_backup_policy == "deterministic_primary_human_paced_backup":
        self_reconciled_scheduler_issues.add("report_missing_field:human_paced_backup_policy")
    if human_paced_backup_enabled is True:
        self_reconciled_scheduler_issues.add("report_missing_field:human_paced_backup_enabled")
    if human_paced_backup_script_exists is True:
        self_reconciled_scheduler_issues.add("report_missing_field:human_paced_backup_script_exists")
    scheduler_issues = [
        issue for issue in scheduler_issues_raw if issue not in self_reconciled_scheduler_issues
    ]
    scheduler_daily_health_ok = (
        (scheduler.get("status") == "ok" and count(scheduler.get("issue_count")) == 0)
        or (bool(daily_job) and not scheduler_issues)
        or (
            bool(daily_job)
            and scheduler.get("status") == "review"
            and bool(scheduler_issues_raw)
            and not scheduler_issues
        )
    )
    sync_report_age_hours = epoch_age_hours(sync.get("finished_at"))
    if sync_report_age_hours is None and mtime(sync_path) > 0:
        sync_report_age_hours = epoch_age_hours(mtime(sync_path))
    daily_started_epoch = iso_epoch_seconds(daily.get("started_at"))
    daily_ended_epoch = iso_epoch_seconds(daily.get("ended_at"))
    sync_started_epoch = epoch_seconds(sync.get("started_at"))
    sync_finished_epoch = epoch_seconds(sync.get("finished_at"))
    sync_report_duration_seconds = (
        round(sync_finished_epoch - sync_started_epoch, 3)
        if sync_started_epoch is not None and sync_finished_epoch is not None
        else None
    )
    if sync_finished_epoch is not None and daily_ended_epoch is not None:
        sync_newer_than_daily = sync_finished_epoch >= daily_ended_epoch
    else:
        sync_newer_than_daily = mtime(sync_path) > mtime(daily_path)
    sync_report_timing_issues: list[str] = []
    timing_tolerance_seconds = 1.0
    if sync_report_duration_seconds is not None and sync_report_duration_seconds < -timing_tolerance_seconds:
        sync_report_timing_issues.append(f"sync_negative_duration_seconds={sync_report_duration_seconds}")
    if daily_started_epoch is not None and sync_started_epoch is not None:
        sync_started_before_daily_start_seconds = round(daily_started_epoch - sync_started_epoch, 3)
        if sync_started_before_daily_start_seconds > timing_tolerance_seconds:
            sync_report_timing_issues.append(
                f"sync_started_before_daily_start_seconds={sync_started_before_daily_start_seconds}"
            )
    else:
        sync_started_before_daily_start_seconds = None
    if daily_ended_epoch is not None and sync_started_epoch is not None:
        sync_started_after_daily_end_seconds = round(sync_started_epoch - daily_ended_epoch, 3)
        if sync_started_after_daily_end_seconds > timing_tolerance_seconds:
            sync_report_timing_issues.append(
                f"sync_started_after_daily_end_seconds={sync_started_after_daily_end_seconds}"
            )
    else:
        sync_started_after_daily_end_seconds = None
    if daily_ended_epoch is not None and sync_finished_epoch is not None:
        sync_finished_after_daily_end_seconds = round(sync_finished_epoch - daily_ended_epoch, 3)
        if sync_finished_after_daily_end_seconds > timing_tolerance_seconds:
            sync_report_timing_issues.append(
                f"sync_finished_after_daily_end_seconds={sync_finished_after_daily_end_seconds}"
            )
    else:
        sync_finished_after_daily_end_seconds = None
    daily_wrapper_problem = (
        not scheduler_daily_health_ok
        or daily.get("sync_report_status") != "ok"
        or any(steps.get(step) != "ok" for step in required_steps if step in steps)
    )
    wrapper_reported_success = (
        daily.get("status") == "ok"
        and daily.get("return_code") == 0
        and daily.get("failed_step") in {None, ""}
    )
    sync_failure_class = str(sync.get("export_failure_class") or "").strip()
    auth401_with_seed_failure = (
        sync_failure_class == "baselane_login_auth_401"
        and session_seed_status not in {"", "ok", "not_started", "skipped"}
        and not current_auth_verified
    )
    login_wait_reason = str(login_wait.get("reason") or "").strip()
    login_wait_status = str(login_wait.get("status") or "").strip()
    login_wait_current_url = str(login_wait.get("current_url") or "").strip()
    daily_sync_auth_blocker_reason = None
    if current_auth_verified:
        # A successful current GraphQL probe is stronger evidence than a stale
        # browser-login artifact from an earlier run.
        daily_sync_auth_blocker_reason = None
    elif login_wait_reason == "baselane_login_recaptcha_required" or login_wait.get("recaptcha_present") is True:
        daily_sync_auth_blocker_reason = "baselane_login_recaptcha_required"
    elif (
        login_wait.get("ok") is False
        and login_wait_status == "review"
        and (
            login_wait.get("login_inputs_present") is True
            or "/login" in login_wait_current_url
            or login_wait_reason in BASELANE_LOGIN_REQUIRED_REASONS
        )
    ):
        daily_sync_auth_blocker_reason = "baselane_login_required"
    elif str(sync.get("reason") or "").strip() in BASELANE_LOGIN_REQUIRED_REASONS:
        daily_sync_auth_blocker_reason = "baselane_login_required"
    elif sync_failure_class == "baselane_login_auth_401":
        daily_sync_auth_blocker_reason = "baselane_login_auth_401"
    elif cdp_auth_recovery.get("manual_auth_required") is True:
        manual_auth_reason = str(cdp_auth_recovery.get("manual_auth_reason") or "").strip()
        daily_sync_auth_blocker_reason = (
            "baselane_login_required"
            if manual_auth_reason in BASELANE_LOGIN_REQUIRED_REASONS
            else "baselane_manual_auth_required"
        )
    use_sync_report_for_daily_health = (
        sync_newer_than_daily
        and sync.get("status") == "ok"
        and sync_report_age_hours is not None
        and daily_wrapper_problem
    )
    use_post_wrapper_successful_sync_for_daily_health = (
        sync_newer_than_daily
        and sync.get("status") == "ok"
        and sync_report_age_hours is not None
        and daily_wrapper_problem
        and not wrapper_reported_success
    )
    use_failed_sync_report_for_daily_health = (
        sync_newer_than_daily
        and sync.get("status") in {"failed", "review", "error"}
        and sync_report_age_hours is not None
        and sync_failure_class in {"baselane_login_auth_401"}
        and daily_wrapper_problem
    )
    daily_health_uses_sync_report = (
        use_sync_report_for_daily_health
        or use_post_wrapper_successful_sync_for_daily_health
        or use_failed_sync_report_for_daily_health
    )
    if use_sync_report_for_daily_health or use_post_wrapper_successful_sync_for_daily_health:
        sync_report_timing_issues = []
    daily_health_age_hours = (
        sync_report_age_hours
        if daily_health_uses_sync_report
        else daily_run_age_hours
    )
    daily_health_age_source = (
        "sync_report_finished_at"
        if daily_health_uses_sync_report
        else "daily_run_ended_at"
    )
    effective_sync_report_status = (
        "ok"
        if (use_sync_report_for_daily_health or use_post_wrapper_successful_sync_for_daily_health)
        else daily.get("sync_report_status")
    )
    standalone_recovery_sync_ok = (
        use_sync_report_for_daily_health
        and sync.get("status") == "ok"
        and sync_finished_epoch is not None
        and (daily_ended_epoch is None or sync_finished_epoch >= daily_ended_epoch)
    )
    wrapper_recovered_by_standalone_sync = standalone_recovery_sync_ok and daily_wrapper_problem
    wrapper_failure_window_hours = 24.0
    wrapper_failure_count, wrapper_failure_records = recent_deterministic_wrapper_failures(
        daily_history_path,
        wrapper_failure_window_hours,
    )
    wrapper_failure_last_record = wrapper_failure_records[-1] if wrapper_failure_records else None
    recovered_sync_repeat_count = wrapper_failure_count if wrapper_recovered_by_standalone_sync else 0
    daily_finished_at = daily.get("finished_at") or daily.get("ended_at")
    model_ready = local_model_ok(model)
    required_step_issues = []
    for step in required_steps:
        if step not in steps:
            continue
        step_status = str(steps.get(step) or "").strip()
        if step_status == "ok":
            continue
        if step == "local_model_preflight" and model_ready:
            continue
        if (
            use_sync_report_for_daily_health
            and step == "deterministic_sync"
            and (standalone_recovery_sync_ok or step_status == "review_nonfatal")
            and sync.get("status") == "ok"
        ):
            continue
        required_step_issues.append(f"{step}:{step_status or 'missing_status'}")
    if use_sync_report_for_daily_health:
        scheduler_issues = [
            issue
            for issue in scheduler_issues
            if not (
                issue.startswith("report_unexpected_value:sync_report_status=")
                or (
                    standalone_recovery_sync_ok
                    and (
                        issue.startswith("unexpected_report_status:")
                        or issue.startswith("report_unexpected_value:return_code=")
                    )
                )
            )
        ]
    actual_sync_status = sync.get("status")
    display_sync_report_status = effective_sync_report_status or actual_sync_status
    if actual_sync_status not in {None, "missing", "unreadable", "ok"}:
        display_sync_report_status = actual_sync_status
    wrapper_consistency_issues: list[str] = []
    if wrapper_reported_success and required_step_issues:
        wrapper_consistency_issues.append(
            f"wrapper_ok_but_required_steps_not_ok={','.join(required_step_issues)}"
        )
    if wrapper_reported_success and missing_steps:
        wrapper_consistency_issues.append(
            f"wrapper_ok_but_required_steps_missing={','.join(missing_steps)}"
        )
    if wrapper_reported_success and display_sync_report_status not in {None, "", "ok"}:
        wrapper_consistency_issues.append(
            f"wrapper_ok_but_sync_report_status={display_sync_report_status}"
        )
    effective_status = daily.get("status")
    effective_return_code = daily.get("return_code")
    effective_failed_step = daily.get("failed_step")
    if wrapper_recovered_by_standalone_sync:
        effective_status = "ok"
        effective_return_code = 0
        effective_failed_step = None
    elif use_failed_sync_report_for_daily_health:
        effective_status = "review"
        effective_return_code = 1
        effective_failed_step = "baselane_sync_cdp_deterministic"
    if wrapper_consistency_issues:
        effective_status = "review"
        effective_return_code = 1
        if (
            "deterministic_sync" in missing_steps
            or any(issue.startswith("deterministic_sync:") for issue in required_step_issues)
            or display_sync_report_status not in {None, "", "ok"}
        ):
            effective_failed_step = "baselane_sync_cdp_deterministic"
        elif (
            "local_model_preflight" in missing_steps
            or any(issue.startswith("local_model_preflight:") for issue in required_step_issues)
        ):
            effective_failed_step = "baselane_local_model_preflight"
        else:
            effective_failed_step = "baselane_daily_wrapper_consistency"
    deterministic_sync_original_status = (
        str(daily.get("deterministic_sync_original_status") or "").strip()
        or str(steps.get("deterministic_sync") or "").strip()
        or None
    )
    daily_recovery_status = str(daily.get("deterministic_sync_recovery_status") or "").strip()
    daily_recovered_by = str(daily.get("deterministic_sync_recovered_by") or "").strip()
    daily_recovery_report = str(daily.get("deterministic_sync_recovery_report") or "").strip()
    deterministic_sync_recovery_required = (
        deterministic_sync_original_status not in {None, "ok"}
        or daily.get("failed_step") == "baselane_sync_cdp_deterministic"
        or daily.get("sync_report_status") not in {None, "", "ok"}
    )
    deterministic_sync_recovery_status = "not_required"
    deterministic_sync_recovered_by = None
    deterministic_sync_recovery_report = None
    if wrapper_recovered_by_standalone_sync:
        deterministic_sync_recovery_status = "recovered_by_newer_successful_sync"
        deterministic_sync_recovered_by = "baselane_sync_cdp_report"
        deterministic_sync_recovery_report = str(reports / "baselane_sync_cdp_report.json")
    elif daily_recovery_status:
        deterministic_sync_recovery_status = daily_recovery_status
        deterministic_sync_recovered_by = daily_recovered_by or None
        deterministic_sync_recovery_report = daily_recovery_report or None
        deterministic_sync_recovery_required = bool(daily.get("deterministic_sync_recovery_required", True))
    elif deterministic_sync_recovery_required:
        deterministic_sync_recovery_status = "required"
    effective_steps = dict(steps)
    if wrapper_recovered_by_standalone_sync:
        effective_steps["deterministic_sync"] = "ok"
    if model_ready:
        effective_steps["local_model_preflight"] = "ok"
    if effective_failed_step == "baselane_local_model_preflight" and (model_ready or not REQUIRE_LOCAL_MODEL_PREFLIGHT):
        if display_sync_report_status not in {None, "", "ok"} or export_guard_status not in {None, "", "ok", "missing"}:
            effective_failed_step = "baselane_sync_cdp_deterministic"
        else:
            effective_status = "ok"
            effective_return_code = 0
            effective_failed_step = None
    source_index_state, source_index_path = source_index_status(root, export_guard, login_export)
    split_step_expected = sync.get("split_exit") == 0 or sync.get("split_subprocess_attempted") is True
    split_mismatch_count = count(split.get("output_mismatch_count"))
    split_unresolved_count = count(split.get("unresolved_property_count"))
    split_write_attempted = split.get("write_attempted") is True
    split_current_count = count(split.get("output_current_count"))
    split_missing_count = count(split.get("output_missing_count"))
    split_stale_count = count(split.get("output_stale_count"))
    split_unreadable_count = count(split.get("output_unreadable_count"))
    source_cash_checked_property_count = count(source_cash.get("checked_property_count"))
    source_cash_reported_scope_expected_count = count(source_cash.get("split_scope_expected_property_count"))
    source_cash_reported_scope_missing_count = count(source_cash.get("split_scope_missing_property_count"))
    source_cash_scope_expected_count = (
        source_cash_reported_scope_expected_count
        if source_cash_reported_scope_expected_count > 0
        else split_current_count
        if split_step_expected and split_current_count > 0
        else None
    )
    source_cash_scope_gap_count = (
        source_cash_reported_scope_missing_count
        if source_cash_reported_scope_expected_count > 0
        else max(0, source_cash_scope_expected_count - source_cash_checked_property_count)
        if source_cash_scope_expected_count is not None
        else 0
    )
    source_cash_checked_properties = [
        str(value)
        for value in (source_cash.get("checked_properties") or source_cash.get("checked_properties_bounded") or [])
        if str(value or "").strip()
    ]
    split_current_properties = [
        str(value)
        for value in (split.get("output_current_properties") or [])
        if str(value or "").strip()
    ]
    source_cash_scope_missing_properties = [
        str(value)
        for value in (source_cash.get("split_scope_missing_properties_bounded") or [])
        if str(value or "").strip()
    ]
    source_cash_checked_property_names_complete = (
        len(source_cash_checked_properties) == source_cash_checked_property_count
        if source_cash_checked_properties
        else False
    )
    if (
        not source_cash_scope_missing_properties
        and split_current_properties
        and source_cash_checked_properties
        and source_cash_checked_property_names_complete
    ):
        checked_property_keys = {normalize_property_name(value) for value in source_cash_checked_properties}
        source_cash_scope_missing_properties = [
            value
            for value in split_current_properties
            if normalize_property_name(value) not in checked_property_keys
            and not any(token_subset_match(value, checked_value) for checked_value in source_cash_checked_properties)
        ]
    source_cash_violation_count = count(source_cash.get("violation_count"))
    source_cash_missing_row_count = count(source_cash.get("missing_row_count"))
    source_cash_missing_month_column_count = count(source_cash.get("missing_month_column_count"))
    source_cash_report_age_hours = report_age_hours(source_cash, source_cash_path)
    source_cash_report_fresh = fresh_report(
        source_cash,
        source_cash_path,
        DAILY_SOURCE_CASH_BALANCE_MAX_AGE_HOURS,
    )
    source_cash_status_ok = source_cash.get("status") == "ok"
    if (
        effective_failed_step == "baselane_hemlane_auto_tag_source_fix"
        and not hemlane_auto_tag_issue
        and (not source_cash_status_ok or source_cash_scope_gap_count)
    ):
        # The wrapper can retain an earlier Hemlane no-op failure after the
        # standalone auto-tag report has become clean. Keep it as historical
        # wrapper evidence, but route the current failure to the source-cash
        # condition that still blocks the pipeline.
        effective_failed_step = "baselane_daily_source_cash_balance"
    first_day_pm_fee_count = count(first_day_pm_fee.get("first_day_pm_fee_count"))
    first_day_pm_fee_status_ok = first_day_pm_fee.get("status") in {"ok", "missing"}
    pm_fee_duplicate_lane_count = count(pm_fee_duplicate_lane.get("issue_count"))
    pm_fee_duplicate_lane_status_ok = pm_fee_duplicate_lane.get("status") in {"ok", "missing"}
    monthly_statements_gate_status = str(monthly_statements_gate.get("status") or "").strip() or "missing"
    monthly_statements_gate_reason = (
        monthly_statements_gate.get("reason")
        or monthly_statements_gate.get("download_error_class")
        or monthly_statements_gate.get("download_error")
        or monthly_statements_gate.get("error")
    )
    monthly_statement_expected_wait = monthly_statements_expected_wait(monthly_statements_gate)
    monthly_statement_gate_issue = (
        monthly_statements_gate_status in {"failed", "error", "unreadable"}
        or (monthly_statements_gate_status == "review" and not monthly_statement_expected_wait)
    )
    assetrail_push_status = str(assetrail_push.get("status") or "").strip()
    assetrail_push_skipped_due_sync = assetrail_push_status == "skipped_sync_not_clean"
    sync_clean_for_assetrail = (
        effective_sync_report_status == "ok"
        and display_sync_report_status in {None, "", "ok"}
        and not any(issue.startswith("deterministic_sync:") for issue in required_step_issues)
    )
    assetrail_push_disabled = (
        assetrail_push_status == "skipped_external_push_not_enabled"
        or str(assetrail_push.get("reason") or "") == "BASELANE_ASSETRAIL_PUSH_ENABLED=0"
    )
    assetrail_push_issue = (
        not assetrail_push_disabled
        and
        not (assetrail_push_skipped_due_sync and not sync_clean_for_assetrail)
        and (
            assetrail_push_status not in {"", "missing", "unreadable", "verified_current_clean", "committed_and_pushed", "pushed_no_ledger_changes"}
            or str(assetrail_push.get("reason") or "").startswith(("git_", "missing_", "ledger_dirty"))
        )
    )
    assetrail_push_clean = assetrail_push_status in {"verified_current_clean", "committed_and_pushed", "pushed_no_ledger_changes"}
    if assetrail_push_disabled:
        assetrail_live = {"status": "skipped", "reason": "external_push_not_enabled", "issues": []}
    elif use_failed_sync_report_for_daily_health and sync_failure_class == "baselane_login_auth_401":
        assetrail_live = {
            "status": "skipped",
            "reason": "baselane_login_auth_401",
            "ledger_path": assetrail_push.get("ledger_path"),
            "ledger_size_bytes": assetrail_push.get("ledger_size_bytes"),
            "ledger_mtime": assetrail_push.get("ledger_mtime"),
            "issues": [],
        }
    else:
        assetrail_live = assetrail_live_state(assetrail_push)
    assetrail_live_issues = assetrail_live.get("issues") if isinstance(assetrail_live.get("issues"), list) else []
    canonical_ledger_path_raw = str(login_export.get("canonical_path") or export_guard.get("canonical_path") or "").strip()
    filtered_snapshot_path_raw = str(login_export.get("filtered_snapshot") or export_guard.get("filtered_snapshot") or "").strip()
    canonical_ledger_path = Path(canonical_ledger_path_raw) if canonical_ledger_path_raw else None
    filtered_snapshot_path = Path(filtered_snapshot_path_raw) if filtered_snapshot_path_raw else None
    if canonical_ledger_path is not None and not canonical_ledger_path.is_absolute():
        canonical_ledger_path = root / canonical_ledger_path
    if filtered_snapshot_path is not None and not filtered_snapshot_path.is_absolute():
        filtered_snapshot_path = root / filtered_snapshot_path
    canonical_ledger_sha256 = file_sha256(canonical_ledger_path) if canonical_ledger_path else None
    filtered_snapshot_sha256 = file_sha256(filtered_snapshot_path) if filtered_snapshot_path else None
    expected_canonical_sha256 = str(login_export.get("canonical_sha256") or export_guard.get("canonical_sha256") or "").strip()
    expected_canonical_sha256_source = "login_export_or_export_guard" if expected_canonical_sha256 else None
    overlay_ledger_path_raw = str(native_split_overlay.get("ledger") or "").strip()
    overlay_ledger_path = Path(overlay_ledger_path_raw) if overlay_ledger_path_raw else None
    if overlay_ledger_path is not None and not overlay_ledger_path.is_absolute():
        overlay_ledger_path = root / overlay_ledger_path
    overlay_ledger_sha256 = str(native_split_overlay.get("ledger_sha256") or "").strip()
    native_split_overlay_baseline = (
        native_split_overlay.get("status") == "ok"
        and canonical_ledger_path is not None
        and overlay_ledger_path == canonical_ledger_path
        and canonical_ledger_sha256 is not None
        and sha256ish(overlay_ledger_sha256)
        and overlay_ledger_sha256 == canonical_ledger_sha256
    )
    if native_split_overlay_baseline:
        expected_canonical_sha256 = overlay_ledger_sha256
        expected_canonical_sha256_source = "native_split_ledger_overlay"
    post_cleanup_baseline = login_export.get("post_cleanup_baseline") is True
    post_export_canonical_baseline = post_cleanup_baseline or native_split_overlay_baseline
    canonical_ledger_issues: list[str] = []
    accrual_overlay_append_ok = False
    accrual_overlay_check: dict[str, Any] = {"status": "not_checked"}
    if canonical_ledger_path and filtered_snapshot_path and canonical_ledger_sha256 and filtered_snapshot_sha256:
        accrual_overlay_append_ok, accrual_overlay_check = canonical_extra_rows_are_accrual_overlay(
            canonical_ledger_path,
            filtered_snapshot_path,
        )
    if sync.get("status") == "ok" and login_export.get("ok") is True:
        if canonical_ledger_path is None:
            canonical_ledger_issues.append("canonical_ledger_path_missing")
        elif canonical_ledger_sha256 is None:
            canonical_ledger_issues.append("canonical_ledger_unreadable")
        if filtered_snapshot_path_raw and filtered_snapshot_sha256 is None:
            canonical_ledger_issues.append("filtered_snapshot_unreadable")
        if (
            canonical_ledger_sha256
            and sha256ish(expected_canonical_sha256)
            and canonical_ledger_sha256 != expected_canonical_sha256
            and not accrual_overlay_append_ok
        ):
            canonical_ledger_issues.append("canonical_ledger_sha_mismatch")
        if (
            not post_export_canonical_baseline
            and canonical_ledger_sha256
            and filtered_snapshot_sha256
            and canonical_ledger_sha256 != filtered_snapshot_sha256
            and not accrual_overlay_append_ok
        ):
            canonical_ledger_issues.append("canonical_ledger_filtered_snapshot_mismatch")
    issues = [
        blocker
        for blocker in [
            None
            if effective_status == "ok"
            else f"daily_run={effective_status}",
            None
            if effective_return_code == 0
            else f"daily_return_code={effective_return_code}",
            None if "ended_at" in daily else "daily_run_missing_ended_at",
            None if count(daily.get("duration_seconds")) >= 0 and daily.get("duration_seconds") is not None else "daily_run_missing_duration_seconds",
            None if effective_sync_report_status == "ok" else f"daily_sync_report_status={effective_sync_report_status}",
            None if sync.get("status") == "ok" else f"sync={sync.get('status')}",
            None if model_ready or not REQUIRE_LOCAL_MODEL_PREFLIGHT else f"local_model_preflight={model.get('status')}",
            None
            if fresh_generated_at(model) or not REQUIRE_LOCAL_MODEL_PREFLIGHT
            else f"local_model_preflight_stale_hours={iso_age_hours(model.get('generated_at'))}",
            None
            if disk_preflight_status in {"", "ok", "missing"}
            else f"disk_space_preflight={disk_preflight_status}:{','.join(disk_preflight_issues[:2]) or 'review'}",
            None
            if (
                scheduler_daily_health_ok
                or (sync_newer_than_daily and sync.get("status") == "ok" and not scheduler_issues)
            )
            else f"scheduler={scheduler.get('status')}",
            None if not scheduler_issues else f"daily_scheduler_issues={','.join(scheduler_issues)}",
            None if not missing_steps else f"daily_steps_missing={','.join(missing_steps)}",
            None if not required_step_issues else f"daily_steps_not_ok={','.join(required_step_issues)}",
            None if not wrapper_consistency_issues else f"daily_wrapper_inconsistent={','.join(wrapper_consistency_issues)}",
            None if not auth401_with_seed_failure else f"session_seed={session_seed_status}",
            None if human_paced_backup_policy == "deterministic_primary_human_paced_backup" else f"human_paced_backup_policy={human_paced_backup_policy}",
            None if human_paced_backup_enabled is True else "human_paced_backup_disabled",
            None if human_paced_backup_script_exists is True else "human_paced_backup_script_missing",
            None if not sync_report_timing_issues else f"sync_timing_inconsistent={','.join(sync_report_timing_issues[:2])}",
            None if scope_guard.get("status") in {"ok", "missing"} else f"scope_guard={scope_guard.get('status')}",
            None if export_guard_status in {"ok", "missing", "unrelated_newer_guard"} else f"export_guard={export_guard_status}",
            None if source_index_state in {"ok", "missing"} else f"source_transaction_index={source_index_state}",
            None
            if not split_step_expected or split.get("status") not in {"missing", "unreadable"}
            else f"split_freshness_report={split.get('status')}",
            None if not split_step_expected or split_write_attempted else "split_property_csv_write_not_confirmed",
            None if split_mismatch_count == 0 else f"split_property_csv_outputs_not_current={split_mismatch_count}",
            None if split_unresolved_count == 0 else f"split_unresolved_properties={split_unresolved_count}",
            None if source_cash_status_ok else f"source_cash_balance={source_cash.get('status')}",
            None
            if source_cash_report_fresh
            else f"source_cash_balance_stale_hours={source_cash_report_age_hours}",
            None if source_cash_violation_count == 0 else f"source_cash_balance_violations={source_cash_violation_count}",
            None if source_cash_missing_row_count == 0 else f"source_cash_balance_missing_rows={source_cash_missing_row_count}",
            None if source_cash_missing_month_column_count == 0 else f"source_cash_balance_missing_month_columns={source_cash_missing_month_column_count}",
            None
            if source_cash_scope_gap_count == 0
            else f"source_cash_balance_scope_gap={source_cash_scope_gap_count}/{source_cash_scope_expected_count}",
            None if first_day_pm_fee_status_ok else f"first_day_pm_fee_audit={first_day_pm_fee.get('status')}",
            None if first_day_pm_fee_count == 0 else f"first_day_pm_fee_rows={first_day_pm_fee_count}",
            None
            if pm_fee_duplicate_lane_status_ok
            else f"pm_fee_duplicate_lane_audit={pm_fee_duplicate_lane.get('status')}",
            None if pm_fee_duplicate_lane_count == 0 else f"pm_fee_duplicate_lanes={pm_fee_duplicate_lane_count}",
            None
            if not monthly_statement_gate_issue
            else f"monthly_statements_gate={monthly_statements_gate_status}:{monthly_statements_gate_reason or 'review'}",
            None
            if not hemlane_live_transaction_issue
            else f"hemlane_live_transactions={hemlane_live_transaction_status or 'missing'}",
            None
            if not hemlane_auto_tag_issue
            else f"hemlane_auto_tag={hemlane_auto_tag_status or hemlane_auto_tag_step_status or 'missing'}",
            None if not assetrail_push_issue else f"assetrail_push={assetrail_push_status or assetrail_push.get('reason')}",
            None if not assetrail_live_issues else f"assetrail_live={','.join(str(issue) for issue in assetrail_live_issues[:3])}",
            None if not canonical_ledger_issues else f"canonical_ledger={','.join(canonical_ledger_issues)}",
        ]
        if blocker
    ]
    if not issues and wrapper_recovered_by_standalone_sync:
        next_action = (
            "Daily data is current via newer successful sync; cron wrapper failed earlier. "
            "No ledger rerun needed; inspect cron wrapper if this repeats."
        )
    elif not issues:
        next_action = "Daily sync is healthy."
    elif disk_preflight_status not in {"", "ok", "missing"}:
        disk_next_action = str(disk_preflight.get("next_action") or "").strip()
        if disk_next_action:
            next_action = f"{disk_next_action} Then rerun scripts/baselane_cron_run.sh and EOD."
        else:
            next_action = "Free local Dropbox/Windows disk space, rerun scripts/baselane_cron_run.sh, then rerun EOD."
    elif auth401_with_seed_failure:
        next_action = (
            "Fix Baselane CDP auth recovery first: inspect session seed status, hard-refresh/reopen the Baselane CDP tab, "
            "then rerun scripts/baselane_cron_run.sh and EOD."
        )
    elif daily_sync_auth_blocker_reason and (
        sync.get("status") != "ok"
        or effective_sync_report_status != "ok"
        or bool(required_step_issues)
        or bool(missing_steps)
    ):
        if daily_sync_auth_blocker_reason == "baselane_login_recaptcha_required":
            next_action = (
                "Solve Baselane reCAPTCHA in the visible CDP tab, then rerun "
                "bash scripts/baselane_cron_run.sh and python3 scripts/baselane_daily_sync_report.py."
            )
        else:
            next_action = (
                "Authenticate Baselane in the visible CDP tab, then rerun "
                "bash scripts/baselane_cron_run.sh and python3 scripts/baselane_daily_sync_report.py."
            )
    elif current_auth_verified and daily.get("sync_report_status") != "ok":
        next_action = (
            "Baselane auth is currently verified. Run the approved daily sync/ledger normalization, then rerun "
            "python3 scripts/baselane_daily_sync_report.py."
        )
    elif hemlane_live_transaction_issue:
        next_action = (
            "Finish Hemlane auth in the visible Brave/CDP session, rerun scripts/baselane_cron_run.sh, then rerun EOD. "
            "Hemlane-backed source tagging fails closed until live transactions are available."
        )
    elif hemlane_auto_tag_issue:
        next_action = (
            "Inspect reports/baselane_hemlane_auto_tag_report.json, fix the guarded Hemlane source-tag apply blocker, "
            "then rerun scripts/baselane_cron_run.sh. Hemlane-backed source tagging fails closed until live Baselane mutation is safe."
        )
    elif (
        sync.get("interrupted") is True
        or str(sync.get("reason") or "").startswith("interrupted")
    ) and assetrail_push_clean:
        next_action = (
            "Run scripts/baselane_sync_cdp_human_paced.py once, then "
            "scripts/baselane_daily_sync_report.py; AssetRail is already clean."
        )
    elif not source_cash_status_ok or source_cash_scope_gap_count:
        missing_scope_text = (
            f"{source_cash_scope_gap_count} split-scope properties missing from source-cash coverage"
            if source_cash_scope_gap_count
            else "source-cash audit is in review"
        )
        next_action = (
            f"Resolve daily source-cash audit: {missing_scope_text}; open "
            "reports/baselane_daily_source_cash_balance_report.json and fix missing/no-match CF or ECO GL mappings."
        )
    else:
        next_action = "Fix the first daily sync issue, rerun scripts/baselane_cron_run.sh, then rerun EOD."
    return {
        "generated_at": iso_z(),
        "status": "ok" if not issues else "review",
        "job": "baselane-daily-sync",
        "issue_count": len(issues),
        "issues": issues,
        "next_action": next_action,
        "root": str(root),
        "current_workspace_root": current_root_raw,
        "daily_run_workspace_root": daily_run_workspace_root,
        "daily_run_openclaw_root": daily_run_openclaw_root,
        "daily_run_workspace_root_normalized": daily_run_workspace_root_normalized,
        "current_workspace_root_normalized": current_root_text,
        "daily_run_workspace_root_raw_matches_current": daily_run_workspace_root_raw_matches_current,
        "daily_run_workspace_root_matches_current": daily_run_workspace_root_matches_current,
        "daily_run_workspace_root_aliases_current": daily_run_workspace_root_aliases_current,
        "daily_run_foreign_workspace_root": daily_run_foreign_workspace_root,
        "daily_run_report": str(reports / "baselane_daily_run_report.json"),
        "session_seed_status": session_seed_status or None,
        "current_auth_recovery_status": current_auth_recovery.get("status"),
        "current_auth_verified": current_auth_verified,
        "auth401_with_seed_failure": auth401_with_seed_failure,
        "daily_sync_auth_blocker_reason": daily_sync_auth_blocker_reason,
        "cdp_auth_recovery_status": cdp_auth_recovery.get("status"),
        "cdp_auth_recovery_issue_summary": cdp_auth_recovery.get("issue_summary"),
        "cdp_auth_recovery_manual_auth_required": cdp_auth_recovery.get("manual_auth_required") is True,
        "cdp_auth_recovery_generated_at": cdp_auth_recovery.get("generated_at"),
        "daily_run_history": str(daily_history_path),
        "sync_report": str(reports / "baselane_sync_cdp_report.json"),
        "login_wait_report": str(reports / "baselane_login_wait_report.json"),
        "cdp_auth_recovery_report": str(reports / "baselane_cdp_auth_recovery_report.json"),
        "disk_space_preflight_report": str(reports / "baselane_daily_disk_space_preflight_report.json"),
        "disk_space_preflight_status": disk_preflight_status or None,
        "disk_space_preflight_issues": disk_preflight_issues,
        "monthly_statements_gate_report": str(monthly_statements_gate_path),
        "split_freshness_report": str(reports / "split_ledger_public_financials_last.json"),
        "daily_source_cash_balance_report": str(reports / "baselane_daily_source_cash_balance_report.json"),
        "first_day_pm_fee_audit_report": str(reports / "baselane_first_day_pm_fee_audit.json"),
        "pm_fee_duplicate_lane_audit_report": str(reports / "baselane_pm_fee_duplicate_lane_audit.json"),
        "pm_fee_duplicate_lane_audit_csv": str(reports / "baselane_pm_fee_duplicate_lane_audit.csv"),
        "first_day_pm_fee_source_cleanup_plan": str(reports / "baselane_first_day_pm_fee_source_cleanup_plan.json"),
        "assetrail_push_report": str(reports / "baselane_assetrail_push_report.json"),
        "first_day_pm_fee_source_cleanup_actions": str(reports / "baselane_first_day_pm_fee_source_cleanup_actions.csv"),
        "local_model_preflight_report": str(reports / "baselane_local_model_preflight_report.json"),
        "scheduler_audit_report": str(reports / "baselane_scheduler_audit_report.json"),
        "daily_run_status": daily.get("status"),
        "daily_run_return_code": daily.get("return_code"),
        "daily_run_failed_step": daily.get("failed_step"),
        "wrapper_status": daily.get("status"),
        "return_code": effective_return_code,
        "failed_step": effective_failed_step,
        "wrapper_return_code": daily.get("return_code"),
        "wrapper_failed_step": daily.get("failed_step"),
        "effective_status": effective_status,
        "effective_return_code": effective_return_code,
        "effective_failed_step": effective_failed_step,
        "wrapper_recovered_by_standalone_sync": wrapper_recovered_by_standalone_sync,
        "daily_wrapper_failure_window_hours": wrapper_failure_window_hours,
        "daily_wrapper_failure_distinct_run_count": wrapper_failure_count,
        "daily_wrapper_failure_records_bounded": wrapper_failure_records,
        "daily_wrapper_failure_last_record": wrapper_failure_last_record,
        "daily_wrapper_failure_last_ended_at": (
            wrapper_failure_last_record.get("ended_at") if wrapper_failure_last_record else None
        ),
        "daily_wrapper_failure_last_failed_step": (
            wrapper_failure_last_record.get("failed_step") if wrapper_failure_last_record else None
        ),
        "daily_recovered_sync_repeat_count": recovered_sync_repeat_count,
        "deterministic_sync_original_status": deterministic_sync_original_status,
        "deterministic_sync_recovery_required": deterministic_sync_recovery_required,
        "deterministic_sync_recovery_status": deterministic_sync_recovery_status,
        "deterministic_sync_recovered_by": deterministic_sync_recovered_by,
        "deterministic_sync_recovery_report": deterministic_sync_recovery_report,
        "human_paced_backup_policy": human_paced_backup_policy,
        "human_paced_backup_enabled": human_paced_backup_enabled,
        "human_paced_backup_script": human_paced_backup_script,
        "human_paced_backup_script_exists": human_paced_backup_script_exists,
        "wrapper_consistency_issues": wrapper_consistency_issues,
        "started_at": daily.get("started_at"),
        "ended_at": daily.get("ended_at"),
        "finished_at": daily_finished_at,
        "finished_at_source": "daily_run_finished_at" if daily.get("finished_at") else "daily_run_ended_at",
        "daily_run_age_hours": daily_run_age_hours,
        "sync_report_age_hours": sync_report_age_hours,
        "daily_health_age_hours": daily_health_age_hours,
        "daily_health_age_source": daily_health_age_source,
        "daily_health_uses_sync_report": daily_health_uses_sync_report,
        "daily_health_uses_post_wrapper_sync_report": use_post_wrapper_successful_sync_for_daily_health,
        "sync_report_failure_overrides_daily_run": use_failed_sync_report_for_daily_health,
        "duration_seconds": daily.get("duration_seconds"),
        "sync_report_status": display_sync_report_status,
        "sync_report_raw_status": daily.get("sync_report_raw_status"),
        "sync_report_status_source": daily.get("sync_report_status_source"),
        "sync_report_reason": sync.get("reason"),
        "sync_report_failure_class": sync_failure_class or None,
        "sync_report_interrupted": sync.get("interrupted"),
        "sync_report_started_at": sync.get("started_at"),
        "sync_report_finished_at": sync.get("finished_at"),
        "sync_report_duration_seconds": sync_report_duration_seconds,
        "sync_report_timing_issue_count": len(sync_report_timing_issues),
        "sync_report_timing_issues": sync_report_timing_issues,
        "sync_report_started_before_daily_start_seconds": sync_started_before_daily_start_seconds,
        "sync_report_started_after_daily_end_seconds": sync_started_after_daily_end_seconds,
        "sync_report_finished_after_daily_end_seconds": sync_finished_after_daily_end_seconds,
        "sync_status": sync.get("status"),
        "split_report_status": split.get("status"),
        "split_write_attempted": split_write_attempted,
        "split_output_current_count": split_current_count,
        "split_output_missing_count": split_missing_count,
        "split_output_stale_count": split_stale_count,
        "split_output_unreadable_count": split_unreadable_count,
        "split_output_mismatch_count": split_mismatch_count,
        "split_unresolved_property_count": split_unresolved_count,
        "split_unresolved_row_count": count(split.get("unresolved_row_count")),
        "split_unresolved_amount_total": split.get("unresolved_amount_total"),
        "split_unresolved_properties_bounded": split.get("unresolved_properties_bounded") or [],
        "split_deferred_acquisition_property_count": count(split.get("deferred_acquisition_property_count")),
        "split_deferred_acquisition_row_count": count(split.get("deferred_acquisition_row_count")),
        "split_deferred_acquisition_amount_total": split.get("deferred_acquisition_amount_total"),
        "source_cash_balance_status": source_cash.get("status"),
        "source_cash_balance_month": source_cash.get("month"),
        "source_cash_balance_generated_at": source_cash.get("generated_at"),
        "source_cash_balance_report_age_hours": source_cash_report_age_hours,
        "source_cash_balance_max_age_hours": DAILY_SOURCE_CASH_BALANCE_MAX_AGE_HOURS,
        "source_cash_balance_report_fresh": source_cash_report_fresh,
        "source_cash_balance_checked_property_count": source_cash_checked_property_count,
        "source_cash_balance_scope_expected_property_count": source_cash_scope_expected_count,
        "source_cash_balance_scope_gap_count": source_cash_scope_gap_count,
        "source_cash_balance_scope_excluded_property_count": count(source_cash.get("split_scope_excluded_property_count")),
        "source_cash_balance_checked_property_names_complete": source_cash_checked_property_names_complete,
        "source_cash_balance_scope_missing_properties_bounded": source_cash_scope_missing_properties[:25],
        "source_cash_balance_update_count": count(source_cash.get("update_count")),
        "source_cash_balance_violation_count": source_cash_violation_count,
        "source_cash_balance_missing_row_count": source_cash_missing_row_count,
        "source_cash_balance_missing_month_column_count": source_cash_missing_month_column_count,
        "source_cash_balance_violation_properties": source_cash.get("violation_properties") or [],
        "source_cash_balance_report": str(source_cash_path),
        "first_day_pm_fee_audit_status": first_day_pm_fee.get("status"),
        "first_day_pm_fee_month": first_day_pm_fee.get("month"),
        "first_day_pm_fee_scope": first_day_pm_fee.get("scope"),
        "first_day_pm_fee_count": first_day_pm_fee_count,
        "first_day_pm_fee_month_counts": first_day_pm_fee.get("month_counts") or {},
        "first_day_pm_fee_rows_bounded": first_day_pm_fee.get("rows_bounded") or [],
        "first_day_pm_fee_source_cleanup_status": first_day_pm_fee_cleanup.get("status"),
        "first_day_pm_fee_source_cleanup_action_count": count(first_day_pm_fee_cleanup.get("action_count")),
        "first_day_pm_fee_source_cleanup_digest": first_day_pm_fee_cleanup.get("idempotency_digest"),
        "first_day_pm_fee_source_cleanup_actions_csv": str(reports / "baselane_first_day_pm_fee_source_cleanup_actions.csv"),
        "first_day_pm_fee_source_cleanup_plan": str(reports / "baselane_first_day_pm_fee_source_cleanup_plan.json"),
        "pm_fee_duplicate_lane_audit_status": pm_fee_duplicate_lane.get("status"),
        "pm_fee_duplicate_lane_count": pm_fee_duplicate_lane_count,
        "pm_fee_duplicate_lane_month": pm_fee_duplicate_lane.get("month"),
        "pm_fee_duplicate_lane_month_counts": pm_fee_duplicate_lane.get("month_counts") or {},
        "pm_fee_duplicate_lane_rows_bounded": pm_fee_duplicate_lane.get("issues_bounded") or [],
        "pm_fee_duplicate_lane_audit_csv": str(reports / "baselane_pm_fee_duplicate_lane_audit.csv"),
        "monthly_statements_gate_status": monthly_statements_gate_status,
        "monthly_statements_gate_reason": monthly_statements_gate_reason,
        "monthly_statements_gate_action": monthly_statements_gate.get("action"),
        "monthly_statements_gate_expected_wait": monthly_statement_expected_wait,
        "monthly_statements_gate_download_ok": monthly_statements_gate.get("download_ok"),
        "monthly_statements_gate_download_error": monthly_statements_gate.get("download_error"),
        "monthly_statements_gate_captured_unique_count": count(monthly_statements_gate.get("captured_unique_count")),
        "monthly_statements_gate_min_captured_required": count(monthly_statements_gate.get("min_captured_required")),
        "monthly_statements_gate_run_month": monthly_statements_gate.get("run_month"),
        "monthly_statements_gate_target_year": monthly_statements_gate.get("target_year"),
        "monthly_statements_gate_target_month": monthly_statements_gate.get("target_month"),
        "monthly_statement_staging_status": steps.get("monthly_statement_staging"),
        "hemlane_live_transactions_report": str(reports / "hemlane_live_transactions.json"),
        "hemlane_live_transaction_status": hemlane_live_transaction_status or None,
        "hemlane_live_transaction_count": count(hemlane_live_transactions.get("transaction_count")),
        "hemlane_live_transaction_required": hemlane_live_transaction_required,
        "hemlane_live_transaction_issue": hemlane_live_transaction_issue,
        "hemlane_live_transaction_capture_status": (
            (hemlane_live_transactions.get("capture") or {}).get("status")
            if isinstance(hemlane_live_transactions.get("capture"), dict)
            else None
        ),
        "hemlane_auto_tag_report": str(reports / "baselane_hemlane_auto_tag_report.json"),
        "hemlane_auto_tag_status": hemlane_auto_tag_status or hemlane_auto_tag_step_status or None,
        "hemlane_auto_tag_issue": hemlane_auto_tag_issue,
        "hemlane_auto_tag_filtered_approved_count": count(hemlane_auto_tag.get("filtered_approved_count")),
        "hemlane_auto_tag_ready_to_apply_count": count(hemlane_auto_tag.get("ready_to_apply_count")),
        "hemlane_auto_tag_already_applied_count": count(hemlane_auto_tag.get("already_applied_count")),
        "hemlane_auto_tag_applied_count": count(hemlane_auto_tag.get("applied_count")),
        "hemlane_auto_tag_failed_count": count(hemlane_auto_tag.get("failed_count")),
        "assetrail_push_status": assetrail_push.get("status"),
        "assetrail_push_reason": assetrail_push.get("reason"),
        "assetrail_git_head": assetrail_push.get("git_head"),
        "assetrail_git_commit_timestamp": assetrail_push.get("git_commit_timestamp"),
        "assetrail_ledger_git_status": assetrail_push.get("ledger_git_status"),
        "assetrail_ledger_path": assetrail_push.get("ledger_path"),
        "assetrail_live_status": assetrail_live.get("status"),
        "assetrail_live_reason": assetrail_live.get("reason"),
        "assetrail_live_issues": assetrail_live_issues,
        "assetrail_live_ledger_size_bytes": assetrail_live.get("ledger_size_bytes"),
        "assetrail_live_ledger_mtime": assetrail_live.get("ledger_mtime"),
        "assetrail_live_ledger_git_status": assetrail_live.get("ledger_git_status"),
        "assetrail_live_git_head": assetrail_live.get("git_head"),
        "assetrail_live_git_upstream": assetrail_live.get("git_upstream"),
        "assetrail_live_git_upstream_head": assetrail_live.get("git_upstream_head"),
        "assetrail_live_git_upstream_ahead_count": assetrail_live.get("git_upstream_ahead_count"),
        "assetrail_live_git_upstream_behind_count": assetrail_live.get("git_upstream_behind_count"),
        "assetrail_live_temp_ledger_status_count": assetrail_live.get("temp_ledger_git_status_count"),
        "assetrail_live_temp_ledger_statuses_bounded": assetrail_live.get("temp_ledger_git_statuses_bounded") or [],
        "canonical_ledger_path": str(canonical_ledger_path) if canonical_ledger_path else None,
        "canonical_ledger_sha256": canonical_ledger_sha256,
        "expected_canonical_sha256": expected_canonical_sha256 or None,
        "expected_canonical_sha256_source": expected_canonical_sha256_source,
        "filtered_snapshot_path": str(filtered_snapshot_path) if filtered_snapshot_path else None,
        "filtered_snapshot_sha256": filtered_snapshot_sha256,
        "post_cleanup_baseline": post_cleanup_baseline,
        "native_split_overlay_baseline": native_split_overlay_baseline,
        "native_split_overlay_report": str(reports / "baselane_native_split_ledger_overlay_report.json"),
        "native_split_overlay_ledger_sha256": overlay_ledger_sha256 or None,
        "canonical_ledger_accrual_overlay_append_ok": accrual_overlay_append_ok,
        "canonical_ledger_accrual_overlay_extra_row_count": accrual_overlay_check.get("extra_row_count"),
        "canonical_ledger_accrual_overlay_check": accrual_overlay_check,
        "canonical_ledger_issues": canonical_ledger_issues,
        "scope_guard_status": scope_guard.get("status"),
        "export_guard_status": export_guard_status,
        "export_guard_written_at": export_guard_written_at,
        "export_guard_after_daily_run": export_guard_after_daily_run,
        "source_transaction_index_status": source_index_state,
        "source_transaction_index": source_index_path,
        "local_model_status": model.get("status"),
        "local_model_ready": model_ready,
        "local_model_preflight_required_for_daily_sync": REQUIRE_LOCAL_MODEL_PREFLIGHT,
        "local_model_preflight_audit_only_policy": (
            "Local model smoke is audited for deterministic helper health, but it does not prove Baselane export correctness, ledger split integrity, "
            "ECO cash balances, or downstream monthly close safety."
        ),
        "small_model_execution_allowed": model.get("small_model_execution_allowed"),
        "small_model_pipeline_execution_allowed": model.get("small_model_pipeline_execution_allowed"),
        "small_model_task_scoped_execution_allowed": model.get("small_model_task_scoped_execution_allowed"),
        "local_model_execution_decision": model.get("small_model_execution_decision"),
        "local_model_execution_policy": model.get("small_model_execution_policy"),
        "local_model_operational": model.get("local_model_operational"),
        "local_model_operational_model_id": model.get("operational_model_id"),
        "local_model_fallback_smoke_ok": model.get("fallback_smoke_ok"),
        "local_model": model.get("model"),
        "local_model_generated_at": model.get("generated_at"),
        "local_model_report_age_hours": iso_age_hours(model.get("generated_at")),
        "local_model_max_age_hours": LOCAL_MODEL_PREFLIGHT_MAX_AGE_HOURS,
        "local_model_validation_digest": model.get("validation_digest"),
        "scheduler_status": scheduler.get("status"),
        "scheduler_issue_count": scheduler.get("issue_count"),
        "scheduler_daily_health_ok": scheduler_daily_health_ok,
        "daily_report_age_hours": daily_job.get("report_age_hours"),
        "daily_report_age_source": "scheduler_report_file",
        "daily_run_age_source": "daily_run_ended_at",
        "daily_health_age_source": daily_health_age_source,
        "daily_report_max_age_hours": daily_job.get("max_report_age_hours"),
        "daily_scheduler_issues": scheduler_issues,
        "steps": effective_steps,
        "wrapper_steps": steps,
        "daily_missing_step_names": missing_steps,
        "source_statuses": {
            "daily_run": daily.get("status"),
            "sync": sync.get("status"),
            "split_property_csvs": "current" if split_mismatch_count == 0 else "not_current",
            "source_cash_balance": source_cash.get("status"),
            "first_day_pm_fee_audit": first_day_pm_fee.get("status"),
            "pm_fee_duplicate_lane_audit": pm_fee_duplicate_lane.get("status"),
            "monthly_statements_gate": monthly_statements_gate_status,
            "hemlane_live_transactions": hemlane_live_transaction_status or hemlane_step_status or "not_required",
            "hemlane_auto_tag": hemlane_auto_tag_status or hemlane_auto_tag_step_status or "not_required",
            "local_model_preflight": model.get("status"),
            "scope_guard": scope_guard.get("status"),
            "export_guard": export_guard_status,
            "source_transaction_index": source_index_state,
            "scheduler": scheduler.get("status"),
        },
    }


def write_json(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            delete=False,
            dir=path.parent,
            encoding="utf-8",
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(path)
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass


def backfill_daily_run_effective_recovery(root: Path, report: dict[str, Any]) -> dict[str, Any]:
    if report.get("wrapper_recovered_by_standalone_sync") is not True:
        return {"attempted": False, "reason": "not_recovered_by_standalone_sync"}
    reports = root / "reports"
    daily_path = reports / "baselane_daily_run_report.json"
    daily = read_json(daily_path)
    if daily.get("status") in {"missing", "unreadable"}:
        return {"attempted": False, "reason": f"daily_run_report_{daily.get('status')}", "path": str(daily_path)}
    updates = {
        "effective_status": report.get("effective_status"),
        "effective_return_code": report.get("effective_return_code"),
        "effective_failed_step": report.get("effective_failed_step"),
        "wrapper_recovered_by_standalone_sync": True,
        "daily_recovered_sync_repeat_count": report.get("daily_recovered_sync_repeat_count"),
        "deterministic_sync_recovery_required": report.get("deterministic_sync_recovery_required"),
        "deterministic_sync_recovery_status": report.get("deterministic_sync_recovery_status"),
        "deterministic_sync_recovered_by": report.get("deterministic_sync_recovered_by"),
        "deterministic_sync_recovery_report": report.get("deterministic_sync_recovery_report"),
        "effective_steps": report.get("steps"),
    }
    changed_fields = []
    for key, value in updates.items():
        if key not in daily or daily.get(key) != value:
            daily[key] = value
            changed_fields.append(key)
    if changed_fields:
        write_json(daily_path, daily)
    return {
        "attempted": True,
        "changed": bool(changed_fields),
        "changed_fields": changed_fields,
        "path": str(daily_path),
        "effective_status": daily.get("effective_status"),
        "deterministic_sync_recovery_status": daily.get("deterministic_sync_recovery_status"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a consolidated deterministic Baselane daily sync report.")
    parser.add_argument("--json", action="store_true", help="print machine-readable status JSON")
    parser.add_argument("--root", type=Path, default=Path(__file__).absolute().parents[1])
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    root = args.root
    report = build_report(root)
    report_path = args.report or root / "reports" / "baselane_daily_sync_report.json"
    write_json(report_path, report)
    backfill_daily_run_effective_recovery(root, report)
    print(json.dumps({key: report[key] for key in ("status", "issue_count", "sync_report_status", "local_model_ready")}, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
