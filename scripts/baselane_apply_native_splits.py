#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from contextlib import contextmanager


ROOT = Path(os.environ.get("WORKSPACE_ROOT") or Path(__file__).absolute().parents[1])
DEFAULT_PLAN = ROOT / "reports" / "baselane_native_split_plan.json"
DEFAULT_REPORT = ROOT / "reports" / "baselane_native_split_apply_report.json"
DEFAULT_STATE = ROOT / "scripts" / ".baselane_native_split_apply_state.json"
DEFAULT_EXECUTOR = ROOT / "scripts" / "baselane_do_split.js"
DEFAULT_AUTH_PREFLIGHT = ROOT / "scripts" / "baselane_cdp_auth_recovery.py"
ALLOWED_RULES = {
    "madison_morgan_linen_4_5_6_5",
    "madison_spectrum_6958_equal",
    "madison_spectrum_equal",
    "madison_netflix_equal",
    "madison_hulu_equal",
    "madison_county_waste_equal",
    "hospitable_april_2026_listing_weights",
    "pricelabs_april_2026_listing_weights",
    "no_dao_mortgage_statement_split",
    "lawnstarter_provider_invoice_split",
}
PENDING_SPLIT_ERROR_MARKERS = (
    "is pending and cannot be split",
    "transaction is pending and cannot be split",
)
PIPELINE_LOCK_PATH = Path(os.environ.get("BASELANE_SOURCE_PIPELINE_LOCK", "/tmp/baselane-source-pipeline.lock"))
PIPELINE_LOCK_HELD_ENV = "BASELANE_SOURCE_PIPELINE_LOCK_HELD"


@contextmanager
def exclusive_pipeline_lock(path: Path = PIPELINE_LOCK_PATH) -> Iterator[bool]:
    """Acquire the shared Baselane source-pipeline lock without waiting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def cents(value: object) -> int:
    text = str(value or "0").replace(",", "").replace("$", "").strip() or "0"
    return int(round(float(text) * 100))


def split_digest(record: dict[str, Any]) -> str:
    material = {
        "rule": record.get("rule"),
        "baselane_id": record.get("baselane_id"),
        "amount": record.get("amount"),
        "escrow_native_split_schedule": record.get("escrow_native_split_schedule"),
        "splits": [
            {
                "amount": split.get("amount"),
                "property_id": split.get("property_id"),
                "tag_id": split.get("tag_id"),
                "property": split.get("property"),
                "category": split.get("category"),
            }
            for split in record.get("splits") or []
        ],
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()


def load_state(path: Path) -> dict[str, Any]:
    state = read_json(path)
    if not isinstance(state, dict):
        return {"applied": {}}
    if not isinstance(state.get("applied"), dict):
        state["applied"] = {}
    return state


def hydrate_state_from_report(state: dict[str, Any], report_path: Path) -> dict[str, Any]:
    previous = read_json(report_path)
    if not isinstance(previous, dict) or previous.get("status") != "ok":
        return state
    applied = state.setdefault("applied", {})
    for action in previous.get("actions") or []:
        if not isinstance(action, dict):
            continue
        execution = action.get("execution") if isinstance(action.get("execution"), dict) else {}
        if action.get("status") != "ready" or execution.get("return_code") != 0:
            continue
        record_id = str(action.get("id") or "")
        split_digest = str(action.get("split_digest") or "")
        if not record_id or not split_digest or record_id in applied:
            continue
        applied[record_id] = {
            "applied_at": previous.get("generated_at"),
            "baselane_id": action.get("baselane_id"),
            "rule": action.get("rule"),
            "split_digest": split_digest,
            "response": execution.get("response"),
            "source": "previous_apply_report",
        }
    return state


def validate_record(record: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if record.get("status") != "ready_native_split":
        issues.append("status_not_ready_native_split")
    if record.get("rule") not in ALLOWED_RULES:
        issues.append(f"rule_not_allowed:{record.get('rule')}")
    if not record.get("baselane_id"):
        issues.append("missing_baselane_id")
    splits = record.get("splits") or []
    if len(splits) < 2:
        issues.append("missing_split_children")
    for index, split in enumerate(splits):
        property_id_optional = split.get("property_id_required") is False or split.get("property_scope") == "unassigned_no_dao_mortgage"
        if not split.get("property_id") and not property_id_optional:
            issues.append(f"split_{index}_missing_property_id")
        if not split.get("tag_id"):
            issues.append(f"split_{index}_missing_tag_id")
        if not split.get("amount"):
            issues.append(f"split_{index}_missing_amount")
    if splits and cents(record.get("amount")) != sum(cents(split.get("amount")) for split in splits):
        issues.append("split_amounts_do_not_sum_to_parent")
    if record.get("rule") == "no_dao_mortgage_statement_split":
        escrow_schedule = record.get("escrow_native_split_schedule") if isinstance(record.get("escrow_native_split_schedule"), dict) else {}
        has_escrow_split = any(
            split.get("category") in {"Insurance", "Taxes"} and cents(split.get("amount")) != 0
            for split in splits
        )
        if has_escrow_split and not escrow_schedule:
            issues.append("missing_escrow_native_split_schedule")
        elif has_escrow_split and (
            not escrow_schedule.get("native_split_update_required")
            or not escrow_schedule.get("native_split_update_ready")
        ):
            issues.append("escrow_native_split_requires_statement_evidence")
    return issues


def executor_payload(record: dict[str, Any]) -> list[dict[str, Any]]:
    date = record.get("iso_date") or record.get("date")
    merchant = str(record.get("merchant") or record.get("rule") or "Baselane split")
    payload = []
    for split in record.get("splits") or []:
        payload.append(
            {
                "amount": float(str(split.get("amount")).replace(",", "")),
                "tagId": str(split.get("tag_id")),
                "propertyId": split.get("property_id"),
                "merchantName": split.get("merchant_name") or f"{merchant} - {split.get('property')}",
                "date": str(date),
            }
        )
    return payload


def record_source_property(record: dict[str, Any]) -> str | None:
    if record.get("source_property"):
        return str(record.get("source_property"))
    for split in record.get("splits") or []:
        if not isinstance(split, dict):
            continue
        if split.get("category") in {"Insurance", "Taxes"} and split.get("property"):
            return str(split.get("property"))
    return None


def build_action(record: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    record_id = str(record.get("id") or "")
    digest = split_digest(record)
    applied = state.get("applied") or {}
    existing = applied.get(record_id)
    issues = validate_record(record)
    if existing and existing.get("split_digest") == digest:
        status = "already_applied"
    elif issues:
        status = "blocked"
    else:
        status = "ready"
    return {
        "id": record_id,
        "rule": record.get("rule"),
        "source_property": record_source_property(record),
        "baselane_id": record.get("baselane_id"),
        "status": status,
        "issues": issues,
        "split_digest": digest,
        "split_count": len(record.get("splits") or []),
        "escrow_native_split_schedule": record.get("escrow_native_split_schedule"),
        "payload": executor_payload(record) if status == "ready" else [],
    }


def action_apply_status(action: dict[str, Any]) -> str:
    if action.get("status") == "already_applied":
        return "already_applied"
    if action.get("status") == "deferred_pending":
        return "deferred_pending"
    execution = action.get("execution") if isinstance(action.get("execution"), dict) else {}
    if action.get("status") == "ready" and execution.get("return_code") == 0:
        return "applied"
    return str(action.get("status") or "unknown")


def execution_pending_deferred(result: dict[str, Any]) -> bool:
    response = result.get("response")
    material = [
        str(result.get("stderr_tail") or ""),
        str(result.get("stdout_tail") or ""),
        json.dumps(response, sort_keys=True, default=str) if response is not None else "",
    ]
    text = "\n".join(material).lower()
    return any(marker in text for marker in PENDING_SPLIT_ERROR_MARKERS)


def escrow_native_split_update_rows(actions: list[dict[str, Any]], *, apply_enabled: bool) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for action in actions:
        schedule = action.get("escrow_native_split_schedule")
        if not isinstance(schedule, dict) or not schedule.get("native_split_update_required"):
            continue
        monthly_splits = schedule.get("monthly_native_splits")
        apply_status = action_apply_status(action)
        rows.append(
            {
                "id": action.get("id"),
                "property": action.get("source_property"),
                "baselane_id": action.get("baselane_id"),
                "action_status": action.get("status"),
                "apply_status": apply_status,
                "apply_enabled": apply_enabled,
                "issues": action.get("issues") or [],
                "statement_path": schedule.get("source_statement_path"),
                "schedule_status": schedule.get("status"),
                "native_split_update_ready": bool(schedule.get("native_split_update_ready")),
                "upstream": schedule.get("upstream"),
                "native_split_update_mode": schedule.get("native_split_update_mode"),
                "effective_start_month": schedule.get("effective_start_month"),
                "effective_end_month": schedule.get("effective_end_month"),
                "schedule_months": schedule.get("schedule_months"),
                "insurance_monthly_amount": schedule.get("insurance_monthly_amount"),
                "taxes_monthly_amount": schedule.get("taxes_monthly_amount"),
                "insurance_native_split_amount": schedule.get("insurance_native_split_amount"),
                "taxes_native_split_amount": schedule.get("taxes_native_split_amount"),
                "escrow_disbursement_amount": schedule.get("escrow_disbursement_amount"),
                "native_split_amount_source": schedule.get("native_split_amount_source"),
                "annual_escrow_reset": schedule.get("annual_escrow_reset"),
                "annual_escrow_reset_reason": schedule.get("annual_escrow_reset_reason"),
                "native_split_schedule_semantics": schedule.get("native_split_schedule_semantics"),
                "monthly_native_splits": monthly_splits if isinstance(monthly_splits, list) else [],
                "live_baselane_native_split_payload_ready": action.get("status") == "ready",
                "live_baselane_native_split_payload_applied": apply_status in {"applied", "already_applied"},
                "payload": action.get("payload") if action.get("status") == "ready" else [],
            }
        )
    return rows


def escrow_native_split_update_summary(actions: list[dict[str, Any]], *, apply_enabled: bool) -> dict[str, Any]:
    updates = escrow_native_split_update_rows(actions, apply_enabled=apply_enabled)
    return {
        "escrow_native_split_update_count": len(updates),
        "escrow_native_split_update_ready_count": sum(
            1 for item in updates if item.get("native_split_update_ready") and item.get("action_status") == "ready"
        ),
        "escrow_native_split_update_blocked_count": sum(
            1 for item in updates if not item.get("native_split_update_ready") or item.get("action_status") == "blocked"
        ),
        "escrow_native_split_update_dry_run_count": sum(
            1 for item in updates if not apply_enabled and item.get("action_status") == "ready"
        ),
        "escrow_native_split_update_applied_count": sum(
            1 for item in updates if item.get("apply_status") == "applied"
        ),
        "escrow_native_split_update_already_applied_count": sum(
            1 for item in updates if item.get("apply_status") == "already_applied"
        ),
        "escrow_native_split_update_properties": sorted(
            {str(item.get("property")) for item in updates if item.get("property")}
        ),
        "escrow_native_split_updates": updates,
    }


def execute_action(action: dict[str, Any], executor: Path, node_bin: str, timeout: int) -> dict[str, Any]:
    command = [
        node_bin,
        str(executor),
        str(action["baselane_id"]),
        json.dumps(action["payload"], separators=(",", ":")),
    ]
    try:
        completed = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return {
            "return_code": 124,
            "stdout_tail": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "",
            "stderr_tail": (exc.stderr or "")[-4000:] if isinstance(exc.stderr, str) else f"timeout after {timeout}s",
            "response": None,
            "timeout_seconds": timeout,
        }
    result: dict[str, Any] = {
        "return_code": completed.returncode,
        "stdout_tail": (completed.stdout or "")[-4000:],
        "stderr_tail": (completed.stderr or "")[-4000:],
    }
    try:
        result["response"] = json.loads(completed.stdout or "{}")
    except Exception:
        result["response"] = None
    return result


def run_auth_preflight(script: Path, node_bin: str, timeout: int) -> dict[str, Any]:
    if os.environ.get("BASELANE_NATIVE_SPLIT_AUTH_PREFLIGHT", "1") == "0":
        return {"enabled": False, "status": "skipped_disabled", "ok": True}
    if not script.is_file():
        return {
            "enabled": True,
            "status": "skipped_missing_script",
            "ok": True,
            "script": str(script),
        }
    env = os.environ.copy()
    env.setdefault("BASELANE_WAIT_MS", "0")
    env.setdefault("BASELANE_AUTH_CONTENT_WAIT_MS", "10000")
    env.setdefault("BASELANE_AUTH_CDP_COMMAND_TIMEOUT_MS", "10000")
    env.setdefault("WORKSPACE_ROOT", str(ROOT))
    env.setdefault("OPENCLAW_ROOT", str(ROOT.parent))
    preflight_timeout = min(timeout, int(os.environ.get("BASELANE_NATIVE_SPLIT_AUTH_PREFLIGHT_TIMEOUT", "60")))
    try:
        completed = subprocess.run(
            [sys.executable, str(script), "--report", str(ROOT / "reports" / "baselane_auth_report.json")],
            text=True,
            capture_output=True,
            timeout=preflight_timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "enabled": True,
            "status": "review",
            "ok": False,
            "return_code": 124,
            "script": str(script),
            "reason": "auth_preflight_timeout",
            "timeout_seconds": preflight_timeout,
            "stdout_tail": (exc.stdout or "")[-2000:] if isinstance(exc.stdout, str) else "",
            "stderr_tail": (exc.stderr or "")[-2000:] if isinstance(exc.stderr, str) else f"timeout after {preflight_timeout}s",
        }
    auth_report_path = ROOT / "reports" / "baselane_auth_report.json"
    auth_report = read_json(auth_report_path)
    auth_error = (auth_report or {}).get("error")
    auth_url = str((auth_report or {}).get("url") or "")
    auth_title = str((auth_report or {}).get("title") or "")
    body_excerpt = str((auth_report or {}).get("body_excerpt") or "")
    loading_only = body_excerpt.strip().lower() in {"loading", "loading...", "loading…"}
    indeterminate_loading = (
        completed.returncode != 0
        and auth_error == "AUTH_REQUIRED"
        and "app.baselane.com" in auth_url
        and not any(marker in auth_url.lower() for marker in ("/login", "/session-expired"))
        and loading_only
    )
    cdp_target_unstable = completed.returncode != 0 and "timeout: Page.enable" in str(auth_error or completed.stderr or "")
    return {
        "enabled": True,
        "status": "ok" if completed.returncode == 0 else ("preflight_target_unstable_allow_executor" if cdp_target_unstable else ("indeterminate_loading_allow_executor" if indeterminate_loading else "review")),
        "ok": completed.returncode == 0 or indeterminate_loading or cdp_target_unstable,
        "return_code": completed.returncode,
        "script": str(script),
        "report": str(auth_report_path),
        "auth_error": auth_error,
        "auth_url": auth_url,
        "auth_title": auth_title,
        "indeterminate_loading": indeterminate_loading,
        "cdp_target_unstable": cdp_target_unstable,
        "stdout_tail": (completed.stdout or "")[-2000:],
        "stderr_tail": (completed.stderr or "")[-2000:],
    }


def apply_plan(
    plan_path: Path,
    report_path: Path,
    state_path: Path,
    executor: Path,
    node_bin: str,
    apply: bool,
    timeout: int,
    auth_preflight: Path = DEFAULT_AUTH_PREFLIGHT,
    baselane_ids: set[str] | None = None,
) -> tuple[int, dict[str, Any]]:
    plan = read_json(plan_path)
    if not isinstance(plan, dict):
        report = {
            "generated_at": iso_z(),
            "status": "review",
            "reason": "missing_or_unreadable_plan",
            "plan": str(plan_path),
            "apply_enabled": apply,
        }
        write_json(report_path, report)
        return 2, report

    state = hydrate_state_from_report(load_state(state_path), report_path)
    records = [record for record in plan.get("records") or [] if isinstance(record, dict)]
    if baselane_ids:
        records = [
            record
            for record in records
            if str(record.get("baselane_id") or "") in baselane_ids
        ]
    actions = [build_action(record, state) for record in records]
    ready = [action for action in actions if action["status"] == "ready"]
    blocked = [action for action in actions if action["status"] == "blocked"]
    applied_now = []
    failures = []
    deferred_pending = []
    auth_preflight_result: dict[str, Any] | None = None

    if apply and ready and not executor.is_file():
        failures.append({"status": "failed", "reason": "missing_executor", "executor": str(executor)})
    elif apply and ready:
        auth_preflight_result = run_auth_preflight(auth_preflight, node_bin, timeout)
        if not auth_preflight_result.get("ok"):
            report = {
                "generated_at": iso_z(),
                "status": "review",
                "reason": "baselane_auth_required",
                "mutation_mode": "apply",
                "apply_enabled": apply,
                "plan": str(plan_path),
                "state_file": str(state_path),
                "executor": str(executor),
                "auth_preflight": auth_preflight_result,
                "allowed_rules": sorted(ALLOWED_RULES),
                "row_count": len(actions),
                "ready_count": len(ready),
                "blocked_count": len(blocked),
                "already_applied_count": sum(1 for action in actions if action["status"] == "already_applied"),
                "applied_count": 0,
                "failure_count": 0,
                "dry_run_count": 0,
                "actions": actions,
            }
            report.update(escrow_native_split_update_summary(actions, apply_enabled=apply))
            write_json(report_path, report)
            return 2, report
        for action in ready:
            result = execute_action(action, executor, node_bin, timeout)
            action["execution"] = result
            if result["return_code"] == 0:
                applied_now.append(action)
                state.setdefault("applied", {})[action["id"]] = {
                    "applied_at": iso_z(),
                    "baselane_id": action["baselane_id"],
                    "rule": action["rule"],
                    "split_digest": action["split_digest"],
                    "response": result.get("response"),
                }
                write_json(state_path, state)
            else:
                if execution_pending_deferred(result):
                    action["status"] = "deferred_pending"
                    action["deferred_reason"] = "baselane_transaction_pending_cannot_split"
                    deferred_pending.append(action)
                    continue
                failures.append(action)
                break
    if apply and state.get("applied"):
        write_json(state_path, state)

    report = {
        "generated_at": iso_z(),
        "status": "failed" if failures else ("review" if blocked else "ok"),
        "mutation_mode": "apply" if apply else "dry_run",
        "apply_enabled": apply,
        "plan": str(plan_path),
        "state_file": str(state_path),
        "executor": str(executor),
        "auth_preflight": auth_preflight_result,
        "allowed_rules": sorted(ALLOWED_RULES),
        "baselane_id_filter": sorted(baselane_ids or []),
        "row_count": len(actions),
        "ready_count": len(ready),
        "blocked_count": len(blocked),
        "already_applied_count": sum(1 for action in actions if action["status"] == "already_applied"),
        "applied_count": len(applied_now),
        "deferred_pending_count": len(deferred_pending),
        "failure_count": len(failures),
        "dry_run_count": len(ready) if not apply else 0,
        "actions": actions,
    }
    report.update(escrow_native_split_update_summary(actions, apply_enabled=apply))
    write_json(report_path, report)
    return (1 if failures else (2 if blocked else 0)), report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply guarded Baselane native split plan rows idempotently.")
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--executor", type=Path, default=DEFAULT_EXECUTOR)
    parser.add_argument("--node-bin", default=os.environ.get("NODE_BIN", "node"))
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("BASELANE_NATIVE_SPLIT_TIMEOUT", "300")))
    parser.add_argument(
        "--baselane-id",
        action="append",
        default=[],
        help="Only process the specified Baselane transaction ID; repeat for multiple IDs.",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    apply_enabled = args.apply and os.environ.get("BASELANE_NATIVE_SPLIT_APPLY", "0") == "1"
    lock_held_by_parent = os.environ.get(PIPELINE_LOCK_HELD_ENV) == "1"
    if apply_enabled and not lock_held_by_parent:
        with exclusive_pipeline_lock() as acquired:
            if not acquired:
                print(
                    json.dumps(
                        {
                            "status": "deferred",
                            "reason": "baselane_source_pipeline_lock_held",
                            "lock": str(PIPELINE_LOCK_PATH),
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 2
            rc, report = apply_plan(
                args.plan,
                args.report,
                args.state,
                args.executor,
                args.node_bin,
                apply_enabled,
                args.timeout,
                baselane_ids=set(args.baselane_id),
            )
    else:
        rc, report = apply_plan(
            args.plan,
            args.report,
            args.state,
            args.executor,
            args.node_bin,
            apply_enabled,
            args.timeout,
            baselane_ids=set(args.baselane_id),
        )
    print(
        json.dumps(
            {
                "status": report.get("status"),
                "mutation_mode": report.get("mutation_mode"),
                "ready_count": report.get("ready_count"),
                "applied_count": report.get("applied_count"),
                "deferred_pending_count": report.get("deferred_pending_count"),
                "blocked_count": report.get("blocked_count"),
                "dry_run_count": report.get("dry_run_count"),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
