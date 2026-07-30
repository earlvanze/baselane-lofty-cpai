#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOFTY_CDP_RECOVERY_ACTION = (
    "Hard-refresh or close/open Lofty property-owners tab; authenticate only if still redirected, then rerun the monthly owner review gate."
)
SAFE_MONTHLY_CRON_DRY_RUN_COMMAND = (
    "DRY_RUN=1 SEND_OWNER_EMAILS=0 PUBLISH_LOFTY_PM_UPDATES=0 "
    "RUN_LOFTY_GUARDED_APPLY=1 APPLY_LOFTY_GUARDED_UPDATES=0 bash scripts/baselane_financials_monthly_cron.sh"
)
LIVE_GUARD_CAPTURE_ACTION = (
    "Auth Lofty visible tab (3 tries), then refresh live UPDATES.md and FINANCIALS.md guard evidence through the safe monthly dry-run. "
    f"Run `{SAFE_MONTHLY_CRON_DRY_RUN_COMMAND}`; this keeps email, Lofty PM publish, and guarded live writes disabled."
)


CSV_FIELDS = [
    "property_name",
    "status",
    "blockers",
    "next_actions",
    "next_action_stage",
    "next_action_file",
    "next_action_command",
    "next_action_detail",
    "updates_md",
    "financials_md",
    "update_status",
    "financial_status",
    "update_candidate",
    "financial_candidate",
    "update_approval_target",
    "financial_approval_target",
    "updates_guard_status",
    "financials_guard_status",
    "live_update_status",
    "live_update_snapshot",
    "live_financial_status",
    "live_financial_snapshot",
    "update_guard_artifact_command",
    "update_guard_capture_command",
    "update_guard_check_command",
    "financial_guard_artifact_command",
    "financial_guard_capture_command",
    "financial_guard_check_command",
]


def no_primary_blocker() -> dict[str, Any]:
    return {
        "artifact": None,
        "blocker": None,
        "class": None,
        "hold": None,
        "id": "none",
        "next_action": "No action required; owner email gate is open.",
        "summary": None,
    }


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "missing", "path": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "unreadable", "path": str(path), "error": str(exc)}
    if isinstance(data, dict):
        return data
    return {"status": "unreadable", "path": str(path), "error": "not a JSON object"}


def count(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def safe_candidate_approval_review_is_rent_roll_hold_only(report: dict[str, Any]) -> bool:
    if report.get("status") != "review":
        return False
    issues = [str(issue or "") for issue in (report.get("issues") or [])]
    if not issues or any(not issue.startswith("rent_roll_source_") for issue in issues):
        return False
    property_count = count(report.get("property_count"))
    if property_count <= 0:
        return False
    status_counts = report.get("status_counts") if isinstance(report.get("status_counts"), dict) else {}
    financial_complete = count(report.get("approved_financial_count")) >= property_count
    update_blocked = count(status_counts.get("update.blocked")) >= property_count
    no_bad_financial_statuses = not any(
        str(key).startswith("financial.") and str(key) not in {"financial.approved", "financial.already_approved", "financial.would_approve"}
        for key, value in status_counts.items()
        if count(value) > 0
    )
    return financial_complete and update_blocked and no_bad_financial_statuses


def update_approval_deferred_by_rent_roll(approval: dict[str, Any]) -> bool:
    update = approval.get("update") if isinstance(approval.get("update"), dict) else {}
    return update.get("status") == "blocked" and update.get("reason") == "rent_roll_source_not_current"


def stable_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def monthly_readiness_blocked_reason(readiness: dict[str, Any]) -> str:
    actionable = readiness.get("actionable_summary") if isinstance(readiness.get("actionable_summary"), dict) else {}
    primary = actionable.get("primary_blocker") if isinstance(actionable.get("primary_blocker"), dict) else {}
    primary_text = str(primary.get("blocker") or primary.get("class") or "").strip()
    actionable_count = count(actionable.get("actionable_blocker_count"))
    if primary_text:
        return f"owner_email_allowed=false:primary={primary_text},actionable={actionable_count}"
    return f"owner_email_allowed=false:actionable={actionable_count}"


APPLIED_OR_EXCLUDED_STATUSES = {
    "applied",
    "already_applied",
    "already_current",
    "skipped_closed",
    "skipped_sold",
    "excluded_no_live_update_or_email",
}
NON_ACTIVE_APPLY_STATUSES = {
    "excluded_no_live_update_or_email",
    "skipped_closed",
    "skipped_sold",
}


def apply_record_status_effectively_current(section: dict[str, Any], status: str) -> bool:
    if status in APPLIED_OR_EXCLUDED_STATUSES:
        return True
    if status != "ready":
        return False
    check = section.get("check") if isinstance(section.get("check"), dict) else {}
    return check.get("ok") is True and count(check.get("return_code")) == 0


def guarded_apply_effectively_current(guarded_apply: dict[str, Any]) -> bool:
    if guarded_apply.get("status") != "ok" or count(guarded_apply.get("issue_count")):
        return False
    if guarded_apply.get("apply") is True:
        return True
    records = guarded_apply.get("records")
    if not isinstance(records, list) or not records:
        return False
    active_record_count = 0
    for record in records:
        if not isinstance(record, dict):
            return False
        update = record.get("updates") if isinstance(record.get("updates"), dict) else {}
        financial = record.get("financials") if isinstance(record.get("financials"), dict) else {}
        update_status = str(update.get("status") or "")
        financial_status = str(financial.get("status") or "")
        if update_status in NON_ACTIVE_APPLY_STATUSES and financial_status in NON_ACTIVE_APPLY_STATUSES:
            continue
        active_record_count += 1
        if not apply_record_status_effectively_current(update, update_status):
            return False
        if not apply_record_status_effectively_current(financial, financial_status):
            return False
    return active_record_count > 0


def live_artifacts_verified(summary: dict[str, Any]) -> bool:
    return (
        summary.get("live_update_target_count", 0) > 0
        and summary.get("live_update_check_ok_count", 0) >= summary.get("live_update_target_count", 0)
        and summary.get("live_update_mismatch_count", 0) == 0
        and summary.get("live_financial_target_count", 0) > 0
        and summary.get("live_financial_check_ok_count", 0) >= summary.get("live_financial_target_count", 0)
        and summary.get("live_financial_mismatch_count", 0) == 0
    )


def publish_effectively_verified(publish: dict[str, Any], summary: dict[str, Any]) -> bool:
    if publish.get("status") != "ok":
        return False
    if not live_artifacts_verified(summary):
        return False
    failure_count = (
        count(publish.get("issue_count"))
        + summary.get("publish_failed_count", 0)
        + summary.get("updates_publish_failed_count", 0)
        + summary.get("financial_publish_failed_count", 0)
    )
    if failure_count:
        return False
    if publish.get("apply") is True:
        return True
    if not summary.get("publish_has_apply_evidence"):
        return True
    attempted_count = summary.get("publish_result_count", 0) + summary.get("financial_publish_result_count", 0)
    expected_count = summary.get("publish_property_count", 0)
    return bool(expected_count and attempted_count >= expected_count and live_artifacts_verified(summary))


def normalize_blocker(blocker: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(blocker)
    blocker_class = str(normalized.get("class") or normalized.get("id") or normalized.get("blocker") or "").strip()
    blocker_text = str(normalized.get("blocker") or blocker_class).strip()
    summary_by_class = {
        "monthly_comms.rent_roll_gap_review.review": "Hemlane rent-roll evidence is stale or blocked; hold owner email and Lofty PM publish.",
        "monthly_comms.rent_roll_gap_approval_coverage.review": "Rent-roll gap approvals are incomplete; hold owner email and Lofty PM publish.",
        "operational.monthly_bank_statement.not_ok": "Monthly Baselane bank statements are not captured and verified yet.",
        "operational.yhome_operating_cash.not_ok": "Yhome Transition Reconciliation operating-cash columns need a gated apply/verify.",
        "operational.local_model_preflight.not_ok": "Local qwen model preflight is not current and passing.",
        "operational.public_path_guard.not_ok": "Dropbox public-path guard found non-canonical owner statement/update paths.",
        "operational.tenant_ledger_folder_guard.not_ok": "Tenant ledger folder guard found misplaced or non-canonical ledger files.",
        "owner_review.guarded_apply_not_applied": "Reviewed owner updates and FINANCIALS approvals have not been applied to canonical Dropbox files yet.",
        "lofty_pm_publish.review": "Lofty PM live listing publish has not completed cleanly for active targets.",
    }
    normalized.setdefault("class", blocker_class or None)
    normalized.setdefault("blocker", blocker_text or None)
    normalized.setdefault("id", blocker_class or "none")
    normalized.setdefault("summary", summary_by_class.get(blocker_class) or blocker_text or None)
    normalized.setdefault("next_action", normalized.get("action"))
    normalized.setdefault("hold", "Lofty PM publish and investor email")
    return normalized


def readiness_primary_blocker(readiness: dict[str, Any]) -> dict[str, Any]:
    actionable = readiness.get("actionable_summary") if isinstance(readiness.get("actionable_summary"), dict) else {}
    primary = actionable.get("primary_blocker") if isinstance(actionable.get("primary_blocker"), dict) else {}
    if not primary:
        return {}
    normalized = normalize_blocker(primary)
    normalized["action"] = normalized.get("action") or normalized.get("next_action") or "Resolve the monthly readiness primary blocker before publish/email."
    normalized["artifact"] = normalized.get("artifact") or normalized.get("file") or normalized.get("work_artifact")
    normalized["command"] = normalized.get("command") or normalized.get("rerun_command")
    normalized["evidence"] = normalized.get("evidence")
    normalized["source"] = "monthly_readiness"
    return normalized


def owner_gate_actionable_summary(
    summary: dict[str, Any],
    readiness: dict[str, Any],
    blockers: list[str],
    property_review_count: int,
) -> dict[str, Any]:
    primary = readiness_primary_blocker(readiness) if readiness.get("owner_email_allowed") is not True else {}
    if primary.get("class") in {
        "monthly_review.skipped_exclusion_count_mismatch",
        "monthly_review.publish_exclusion_guard_failed",
    } and summary.get("property_excluded_total_count") == summary.get("publish_excluded_property_count"):
        primary = {}
    downstream_collapsed = False
    if primary:
        downstream_collapsed = True
    elif summary["lofty_pm_tab_count"] < 1:
        primary = {
            "class": "lofty_pm_cdp.review",
            "blocker": "lofty_pm_cdp.review",
            "action": str(summary.get("lofty_next_action") or LOFTY_CDP_RECOVERY_ACTION),
            "artifact": "reports/lofty_cdp_preflight_report.json",
            "source": "owner_review_gate",
        }
        downstream_collapsed = True
    elif summary["pending_update_review_count"] or summary["pending_financial_review_count"]:
        primary = {
            "class": "owner_review.approvals_pending",
            "blocker": "owner_review.approvals_pending",
            "action": "Review the candidate packet and write approved content to each listed approval target.",
            "artifact": "reports/baselane_financials_monthly_review_candidate_packet.md",
            "source": "owner_review_gate",
        }
    elif summary["guard_issue_count"] or not live_artifacts_verified(summary):
        primary = {
            "class": "owner_review.live_guard_capture",
            "blocker": "owner_review.live_guard_capture",
            "action": LIVE_GUARD_CAPTURE_ACTION,
            "artifact": "reports/baselane_monthly_owner_review_gate.csv",
            "source": "owner_review_gate",
        }
    elif not summary.get("guarded_apply_effectively_current"):
        primary = {
            "class": "owner_review.guarded_apply_not_applied",
            "blocker": "owner_review.guarded_apply_not_applied",
            "action": "Run the monthly cron with guarded apply enabled only after review inputs stay clean; keep owner email disabled unless explicitly sending through the reviewed packet workflow.",
            "artifact": "reports/baselane_financials_monthly_guarded_apply.json",
            "source": "owner_review_gate",
        }
    elif not summary["owner_email_allowed"]:
        primary = {
            "class": "owner_email.not_allowed",
            "blocker": "owner_email.not_allowed",
            "action": "Keep owner email disabled until monthly readiness allows send.",
            "artifact": "reports/baselane_financials_monthly_readiness.md",
            "source": "owner_review_gate",
        }
    elif summary["publish_status"] != "ok":
        primary = {
            "class": "lofty_pm_publish.review",
            "blocker": "lofty_pm_publish.review",
            "action": "Review the Lofty PM publish report and keep email disabled until send evidence is clean.",
            "artifact": "reports/baselane_financials_monthly_lofty_pm_publish.json",
            "source": "owner_review_gate",
        }
    elif (
        summary.get("publish_has_apply_evidence")
        and not summary.get("publish_effectively_verified")
    ):
        primary = {
            "class": "lofty_pm_publish.review",
            "blocker": "lofty_pm_publish.review",
            "action": "Fix the Lofty PM publish path, rerun live publish for active targets, and keep owner email disabled until all publish results are clean.",
            "artifact": "reports/baselane_financials_monthly_lofty_pm_publish.json",
            "source": "owner_review_gate",
        }

    if not primary:
        primary = no_primary_blocker()

    return {
        "primary_blocker": primary,
        "actionable_blocker_count": 1 if blockers else 0,
        "audit_blocker_count": len(blockers),
        "property_review_audit_count": property_review_count,
        "property_detail_collapsed": downstream_collapsed and property_review_count > 0,
        "downstream_audit_collapsed": downstream_collapsed,
        "noise_policy": "Use primary_blocker for action; full blocker/property evidence remains in JSON and CSV.",
    }


def owner_gate_actions(actionable_summary: dict[str, Any], summary: dict[str, Any], readiness: dict[str, Any], publish: dict[str, Any]) -> list[str]:
    primary = actionable_summary.get("primary_blocker") if isinstance(actionable_summary.get("primary_blocker"), dict) else {}
    actions: list[str] = []
    if primary.get("action"):
        actions.append(str(primary["action"]))
    if primary.get("artifact"):
        actions.append(f"Open {primary['artifact']} for the current evidence/work queue.")
    if summary["lofty_pm_tab_count"] < 1 and primary.get("class") != "lofty_pm_cdp.review":
        actions.append(
            str(
                summary.get("lofty_next_action")
                or LOFTY_CDP_RECOVERY_ACTION
            )
        )
    if not actionable_summary.get("downstream_audit_collapsed"):
        if summary["pending_update_review_count"] or summary["pending_financial_review_count"]:
            actions.append("Review reports/baselane_financials_monthly_review_candidate_packet.md and copy approved content to each target listed in reports/baselane_financials_monthly_review_manifest.md.")
        if summary["guard_issue_count"] or not live_artifacts_verified(summary):
            actions.append("Authenticate Lofty PM, capture/register live UPDATES.md and FINANCIALS.md for each property guard, then rerun guarded apply.")
    if readiness.get("owner_email_allowed") is not True or publish.get("status") != "ok":
        actions.append("Keep owner email disabled; publish/email remains blocked until readiness, guarded apply, and send evidence are clean.")
    if not actions:
        actions.append("No monthly owner review action needed.")
    deduped: list[str] = []
    for action in actions:
        if action not in deduped:
            deduped.append(action)
    return deduped


def owner_gate_primary_aliases(actionable_summary: dict[str, Any]) -> tuple[dict[str, Any], str | None, str | None]:
    primary = actionable_summary.get("primary_blocker") if isinstance(actionable_summary.get("primary_blocker"), dict) else {}
    if not primary:
        primary = no_primary_blocker()
    if primary.get("id") == "none":
        actionable_summary["primary_blocker"] = primary
        return primary, str(primary.get("next_action") or "").strip() or None, primary.get("hold")
    normalized = normalize_blocker(primary)
    next_action = str(normalized.get("next_action") or normalized.get("action") or "").strip() or None
    hold = str(normalized.get("hold") or "Lofty PM publish and investor email").strip()
    if next_action:
        normalized["next_action"] = next_action
    normalized["hold"] = hold
    actionable_summary["primary_blocker"] = normalized
    return normalized, next_action, hold


def rel(path: object, root: Path) -> str:
    raw = str(path or "").strip()
    if not raw:
        return ""
    candidate = Path(raw)
    try:
        if candidate.is_absolute():
            return str(candidate.relative_to(root))
    except ValueError:
        pass
    return raw


def property_key(record: dict[str, Any]) -> str:
    return str(record.get("property_path") or record.get("property_name") or record.get("match_key") or "").strip()


def normalized_property_name(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def publish_excluded_property_names(publish: dict[str, Any]) -> set[str]:
    raw_names = list(publish.get("excluded_property_names") or [])
    send_inputs = publish.get("send_decision_inputs") if isinstance(publish.get("send_decision_inputs"), dict) else {}
    raw_names.extend(send_inputs.get("excluded_property_names") or [])
    return {normalized_property_name(name) for name in raw_names if normalized_property_name(name)}


def index_records(records: list[Any]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        key = property_key(record)
        if key:
            indexed[key] = record
        name = str(record.get("property_name") or record.get("match_key") or "").strip()
        if name:
            indexed.setdefault(name, record)
    return indexed


def basename(path: object) -> str:
    raw = str(path or "").strip()
    return Path(raw).name if raw else ""


def public_tail(path: object) -> str:
    raw = str(path or "").strip()
    if not raw:
        return ""
    parts = Path(raw).parts
    if "Public" in parts:
        index = parts.index("Public")
        return "/".join(parts[index:])
    return raw


def shell_command(parts: list[object]) -> str:
    return " ".join(shlex.quote(str(part)) for part in parts if str(part or "").strip())


def build_guard_commands(
    comms_workspace: Path,
    updates_md: object,
    financials_md: object,
    live_update_snapshot: object,
    live_financial_snapshot: object,
) -> dict[str, list[str]]:
    updates_guard = comms_workspace / "scripts" / "lofty-updates-guard.py"
    live_file_guard = comms_workspace / "scripts" / "lofty-live-file-guard.py"
    commands: dict[str, list[str]] = {"updates": [], "financials": []}
    if str(updates_md or "").strip():
        commands["updates"].append(shell_command([updates_guard, "artifact-path", updates_md]))
        if str(live_update_snapshot or "").strip():
            commands["updates"].append(
                shell_command(
                    [
                        updates_guard,
                        "capture-fetch",
                        updates_md,
                        live_update_snapshot,
                        "--source",
                        "Lofty PM get-manager-properties updates field",
                    ]
                )
            )
        commands["updates"].append(shell_command([updates_guard, "check", updates_md]))
    if str(financials_md or "").strip():
        commands["financials"].append(shell_command([live_file_guard, "artifact-path", financials_md]))
        if str(live_financial_snapshot or "").strip():
            commands["financials"].append(
                shell_command(
                    [
                        live_file_guard,
                        "capture-fetch",
                        financials_md,
                        live_financial_snapshot,
                        "--source",
                        "Lofty PM get-manager-properties financial data",
                    ]
                )
            )
        commands["financials"].append(shell_command([live_file_guard, "check", financials_md]))
    return commands


def first_command(commands: dict[str, list[str]], group: str, contains: str | None = None) -> str:
    group_commands = commands.get(group) if isinstance(commands.get(group), list) else []
    for command in group_commands:
        text = str(command)
        if contains is None or contains in text:
            return text
    return ""


GUARD_DERIVED_STATUSES = {"guard_failed"}


def candidate_review_blocker(blocker: str) -> bool:
    if not (blocker.startswith("update=") or blocker.startswith("financial=")):
        return False
    _, _, status = blocker.partition("=")
    return status not in GUARD_DERIVED_STATUSES


def derive_next_action(record: dict[str, Any]) -> dict[str, str]:
    blockers = [str(blocker) for blocker in (record.get("blockers") or [])]
    guard_commands = record.get("guard_commands") if isinstance(record.get("guard_commands"), dict) else {}
    if any(blocker.startswith("update=") and candidate_review_blocker(blocker) for blocker in blockers):
        return {
            "next_action_stage": "approve_update_candidate",
            "next_action_file": str(record.get("update_candidate") or record.get("update_approval_target") or ""),
            "next_action_command": "",
            "next_action_detail": "Review the update candidate, then write only approved content to the update approval target.",
        }
    if any(blocker.startswith("financial=") and candidate_review_blocker(blocker) for blocker in blockers):
        return {
            "next_action_stage": "approve_financial_candidate",
            "next_action_file": str(record.get("financial_candidate") or record.get("financial_approval_target") or ""),
            "next_action_command": "",
            "next_action_detail": "Review the FINANCIALS candidate, then write only approved content to the financial approval target.",
        }
    if any(blocker.startswith("live_update=") for blocker in blockers):
        return {
            "next_action_stage": "capture_update_live_guard",
            "next_action_file": str(record.get("live_update_snapshot") or record.get("updates_md") or ""),
            "next_action_command": first_command(guard_commands, "updates", "capture-fetch"),
            "next_action_detail": "Capture/register the Lofty PM live UPDATES.md snapshot, then run the update guard check.",
        }
    if any(blocker.startswith("updates_guard=") for blocker in blockers):
        return {
            "next_action_stage": "check_update_guard",
            "next_action_file": str(record.get("updates_md") or ""),
            "next_action_command": first_command(guard_commands, "updates", "check"),
            "next_action_detail": "Run the UPDATES.md guard check after approval and live capture are in place.",
        }
    if any(blocker.startswith("live_financial=") for blocker in blockers):
        return {
            "next_action_stage": "capture_financial_live_guard",
            "next_action_file": str(record.get("live_financial_snapshot") or record.get("financials_md") or ""),
            "next_action_command": first_command(guard_commands, "financials", "capture-fetch"),
            "next_action_detail": "Capture/register the Lofty PM live FINANCIALS.md snapshot, then run the financial guard check.",
        }
    if any(blocker.startswith("financials_guard=") for blocker in blockers):
        return {
            "next_action_stage": "check_financial_guard",
            "next_action_file": str(record.get("financials_md") or ""),
            "next_action_command": first_command(guard_commands, "financials", "check"),
            "next_action_detail": "Run the FINANCIALS.md guard check after approval and live capture are in place.",
        }
    return {
        "next_action_stage": "",
        "next_action_file": "",
        "next_action_command": "",
        "next_action_detail": "",
    }


def build_property_checklist(
    manifest: dict[str, Any],
    candidate_packet: dict[str, Any],
    safe_approval: dict[str, Any],
    guard_audit: dict[str, Any],
    guarded_apply: dict[str, Any],
    live_updates: dict[str, Any],
    live_financials: dict[str, Any],
    comms_workspace: Path,
    externally_excluded_names: set[str] | None = None,
) -> list[dict[str, Any]]:
    externally_excluded_names = externally_excluded_names or set()
    manifest_records = manifest.get("records") or []
    if not manifest_records and candidate_packet.get("records"):
        manifest_records = candidate_packet.get("records") or []
    candidate_by_key = index_records(candidate_packet.get("records") or [])
    approval_by_key = index_records(safe_approval.get("records") or [])
    guard_by_key = index_records(guard_audit.get("records") or [])
    apply_by_key = index_records(guarded_apply.get("records") or [])
    live_update_by_key = index_records(live_updates.get("records") or [])
    live_financial_by_key = index_records(live_financials.get("records") or [])
    safe_update_reviews_deferred_by_rent_roll = safe_candidate_approval_review_is_rent_roll_hold_only(safe_approval)
    records = []
    for manifest_record in manifest_records:
        if not isinstance(manifest_record, dict):
            continue
        key = property_key(manifest_record)
        name = str(manifest_record.get("property_name") or basename(manifest_record.get("property_path")) or key)
        externally_excluded = (
            normalized_property_name(name) in externally_excluded_names
            or normalized_property_name(basename(key)) in externally_excluded_names
            or normalized_property_name(key) in externally_excluded_names
        )
        candidate = candidate_by_key.get(key) or candidate_by_key.get(name) or {}
        approval = approval_by_key.get(key) or approval_by_key.get(name) or {}
        guard = guard_by_key.get(key) or guard_by_key.get(name) or {}
        apply_record = apply_by_key.get(key) or apply_by_key.get(name) or {}
        live_update = live_update_by_key.get(key) or live_update_by_key.get(name) or {}
        live_financial = live_financial_by_key.get(key) or live_financial_by_key.get(name) or {}
        guard_checks = guard.get("checks") if isinstance(guard.get("checks"), dict) else {}
        update_guard = guard_checks.get("updates") if isinstance(guard_checks.get("updates"), dict) else {}
        financial_guard = guard_checks.get("financials") if isinstance(guard_checks.get("financials"), dict) else {}
        update_apply_status = str((apply_record.get("updates") or {}).get("status") or "")
        financial_apply_status = str((apply_record.get("financials") or {}).get("status") or "")
        update_approval_status = str((approval.get("update") or {}).get("status") or "")
        financial_approval_status = str((approval.get("financial") or {}).get("status") or "")
        update_status = str(manifest_record.get("update_status") or update_apply_status or "unknown")
        financial_status = str(manifest_record.get("financial_status") or financial_apply_status or "unknown")
        safe_approval_applied = safe_approval.get("apply") is True and safe_approval.get("status") in {"ok", "review"}
        update_deferred_by_rent_roll = safe_approval_applied and update_approval_deferred_by_rent_roll(approval)
        if update_apply_status in {"ready", "applied", "already_applied", "already_current"}:
            update_status = update_apply_status
        elif safe_approval_applied and update_approval_status in {"approved", "already_approved"}:
            update_status = "approved"
        elif update_deferred_by_rent_roll or (
            safe_update_reviews_deferred_by_rent_roll and update_approval_deferred_by_rent_roll(approval)
        ):
            update_status = "deferred_rent_roll_source_not_current"
        if financial_apply_status in {"ready", "applied", "already_applied", "already_current"}:
            financial_status = financial_apply_status
        elif financial_approval_status == "already_approved":
            financial_status = "approved"
        elif safe_approval_applied and financial_approval_status in {"approved", "already_approved"}:
            financial_status = "approved"
        skipped_closed = update_status.startswith("skipped_") and financial_status.startswith("skipped_")
        status_excluded = update_status.startswith("excluded_") and financial_status.startswith("excluded_")
        externally_excluded = externally_excluded or status_excluded
        blockers = []
        approved_statuses = {"approved", "ready", "applied", "already_applied", "already_current", "skipped_closed", "deferred_rent_roll_source_not_current"}
        if not externally_excluded and update_status not in approved_statuses:
            blockers.append(f"update={update_status}")
        if not externally_excluded and financial_status not in approved_statuses:
            blockers.append(f"financial={financial_status}")
        if not skipped_closed and not externally_excluded and update_guard.get("status") not in {"ok", None}:
            blockers.append(f"updates_guard={update_guard.get('status')}")
        if not skipped_closed and not externally_excluded and financial_guard.get("status") not in {"ok", None}:
            blockers.append(f"financials_guard={financial_guard.get('status')}")
        if not skipped_closed and not externally_excluded and live_update.get("status") not in {"ok", "registered", "check_ok", "guard_ok"}:
            blockers.append(f"live_update={live_update.get('status') or 'missing'}")
        if not skipped_closed and not externally_excluded and live_financial.get("status") not in {
            "ok",
            "registered",
            "check_ok",
            "guard_ok",
            "guard_ok_live_distribution",
            "guard_ok_no_distribution_target",
            "needs_reconcile",
        }:
            blockers.append(f"live_financial={live_financial.get('status') or 'missing'}")
        actions = []
        if any(candidate_review_blocker(blocker) for blocker in blockers):
            actions.append("review_candidate_and_write_approval_target")
        if any("guard=" in blocker or "live_" in blocker for blocker in blockers):
            actions.append("capture_or_register_lofty_live_guard")
        updates_md = manifest_record.get("updates_md") or guard.get("updates_md")
        financials_md = manifest_record.get("financials_md") or guard.get("financials_md")
        guard_commands = (
            {"updates": [], "financials": []}
            if skipped_closed or externally_excluded
            else build_guard_commands(
                comms_workspace,
                updates_md,
                financials_md,
                live_update.get("snapshot_path"),
                live_financial.get("snapshot_path"),
            )
        )
        record = {
            "property_name": name,
            "property_path": key,
            "status": "skipped_closed" if skipped_closed and not blockers else "excluded_external" if externally_excluded else "ok" if not blockers else "review",
            "blockers": blockers,
            "next_actions": actions,
            "external_exclusion": externally_excluded,
            "updates_md": updates_md,
            "financials_md": financials_md,
            "update_status": update_status,
            "financial_status": financial_status,
            "update_candidate": candidate.get("update_candidate"),
            "financial_candidate": candidate.get("financial_candidate"),
            "update_approval_target": candidate.get("update_approval_target") or manifest_record.get("update_review_target"),
            "financial_approval_target": candidate.get("financial_approval_target") or manifest_record.get("financial_review_target"),
            "updates_guard_status": update_guard.get("status"),
            "financials_guard_status": financial_guard.get("status"),
            "live_update_status": live_update.get("status"),
            "live_update_snapshot": live_update.get("snapshot_path"),
            "live_financial_status": live_financial.get("status"),
            "live_financial_snapshot": live_financial.get("snapshot_path"),
            "guard_commands": guard_commands,
        }
        record.update(derive_next_action(record))
        records.append(record)
    records.sort(key=lambda item: (item["status"] != "review", item["property_name"]))
    return records


def build_report(root: Path) -> dict[str, Any]:
    reports = root / "reports"
    manifest = read_json(reports / "baselane_financials_monthly_review_manifest.json")
    candidate_packet = read_json(reports / "baselane_financials_monthly_review_candidate_packet.json")
    safety_scan = read_json(reports / "baselane_financials_monthly_review_safety_scan.json")
    safe_approval = read_json(reports / "baselane_financials_monthly_safe_candidate_approval.json")
    guard_audit = read_json(reports / "baselane_financials_monthly_guard_audit.json")
    guarded_apply = read_json(reports / "baselane_financials_monthly_guarded_apply.json")
    live_updates = read_json(reports / "baselane_financials_monthly_live_update_capture.json")
    live_financials = read_json(reports / "baselane_financials_monthly_live_financial_capture.json")
    cdp = read_json(reports / "lofty_cdp_preflight_report.json")
    readiness = read_json(reports / "baselane_financials_monthly_readiness.json")
    publish = read_json(reports / "baselane_financials_monthly_lofty_pm_publish.json")
    monthly_run = read_json(reports / "baselane_financials_monthly_run_report.json")
    comms_workspace = Path(os.environ.get("COMMS_WORKSPACE") or root.parent / "workspace-lofty-vp")
    if not comms_workspace.is_dir():
        comms_workspace = root.parent / "workspace-lofty-vp-comms"
    property_checklist = build_property_checklist(
        manifest,
        candidate_packet,
        safe_approval,
        guard_audit,
        guarded_apply,
        live_updates,
        live_financials,
        comms_workspace,
        publish_excluded_property_names(publish),
    )
    property_review_count = sum(1 for record in property_checklist if record.get("status") == "review")
    property_skipped_count = sum(1 for record in property_checklist if str(record.get("status") or "").startswith("skipped_"))
    property_external_excluded_count = sum(1 for record in property_checklist if record.get("status") == "excluded_external")
    property_excluded_total_count = property_skipped_count + property_external_excluded_count
    guard_workflow = guard_workflow_coverage(property_checklist)

    safe_approval_ok = safe_approval.get("status") in {"ok", "ok_dry_run"}
    safe_update_count = count(safe_approval.get("approved_update_count"))
    safe_financial_count = count(safe_approval.get("approved_financial_count"))
    candidate_property_count = count(candidate_packet.get("property_count"))
    applied_safe_approval_status = safe_approval.get("status") in {"ok", "review"} and safe_approval.get("apply") is True
    update_approvals_cover_candidates = applied_safe_approval_status and candidate_property_count > 0 and safe_update_count >= candidate_property_count
    financial_approvals_cover_candidates = applied_safe_approval_status and candidate_property_count > 0 and safe_financial_count >= candidate_property_count
    safe_update_reviews_deferred_by_rent_roll = safe_candidate_approval_review_is_rent_roll_hold_only(safe_approval)
    approvals_cover_candidates = (
        safe_approval_ok
        and candidate_property_count > 0
        and safe_update_count >= candidate_property_count
        and safe_financial_count >= candidate_property_count
    )

    run_month = os.environ.get("RUN_MONTH") or manifest.get("run_month") or publish.get("run_month") or monthly_run.get("run_month")
    summary = {
        "run_month": run_month,
        "property_count": count(manifest.get("property_count") or candidate_packet.get("property_count") or publish.get("property_count")),
        "pending_update_review_count": 0 if approvals_cover_candidates or update_approvals_cover_candidates or safe_update_reviews_deferred_by_rent_roll else count(manifest.get("pending_update_review_count")),
        "pending_financial_review_count": 0 if approvals_cover_candidates or financial_approvals_cover_candidates or safe_update_reviews_deferred_by_rent_roll else count(manifest.get("pending_financial_review_count")),
        "safe_candidate_approval_status": safe_approval.get("status"),
        "safe_candidate_approval_apply": safe_approval.get("apply") is True,
        "safe_update_reviews_deferred_by_rent_roll": safe_update_reviews_deferred_by_rent_roll,
        "approved_update_count": safe_update_count,
        "approved_financial_count": safe_financial_count,
        "candidate_property_count": candidate_property_count,
        "candidate_issue_count": count(candidate_packet.get("issue_count")),
        "candidate_marker_count": count(candidate_packet.get("marker_count")),
        "candidate_financial_gate_issue_count": count(candidate_packet.get("financial_candidate_gate_issue_count")),
        "safety_high_count": count(safety_scan.get("high_count")),
        "safety_medium_count": count(safety_scan.get("medium_count")),
        "safety_missing_count": count(safety_scan.get("missing_count")),
        "guard_issue_count": count(guard_audit.get("issue_count")),
        "guarded_apply": guarded_apply.get("apply") is True,
        "guarded_apply_effectively_current": guarded_apply_effectively_current(guarded_apply),
        "live_update_registered_count": count(live_updates.get("register_count")),
        "live_update_check_ok_count": count(live_updates.get("check_ok_count")),
        "live_update_mismatch_count": count(live_updates.get("mismatch_count")),
        "live_update_unverified_count": count(live_updates.get("unverified_count")),
        "live_update_target_digest": live_updates.get("target_digest"),
        "live_update_target_count": count(live_updates.get("target_count")),
        "live_financial_registered_count": count(live_financials.get("register_count")),
        "live_financial_check_ok_count": count(live_financials.get("check_ok_count")),
        "live_financial_mismatch_count": count(live_financials.get("mismatch_count")),
        "live_financial_unverified_count": count(live_financials.get("unverified_count")),
        "live_financial_target_digest": live_financials.get("target_digest"),
        "live_financial_target_count": count(live_financials.get("target_count")),
        "lofty_pm_tab_count": count(cdp.get("pm_tab_count")),
        "lofty_login_tab_count": count(cdp.get("login_tab_count")),
        "lofty_next_action": cdp.get("next_action"),
        "owner_email_allowed": readiness.get("owner_email_allowed") is True,
        "owner_email_send_lock_status": publish.get("send_lock_status"),
        "owner_email_sent_state_month": publish.get("sent_state_month"),
        "publish_status": publish.get("status"),
        "publish_excluded_property_count": count(publish.get("excluded_property_count")),
        "publish_excluded_payload_file_count": count(publish.get("excluded_payload_file_count")),
        "publish_excluded_owner_email_candidate_count": count(publish.get("excluded_owner_email_candidate_count")),
        "publish_has_apply_evidence": any(
            key in publish
            for key in (
                "apply",
                "property_count",
                "publish_result_count",
                "publish_failed_count",
                "financial_publish_result_count",
                "financial_publish_failed_count",
            )
        ),
        "publish_apply": publish.get("apply") is True,
        "publish_property_count": count(publish.get("property_count")),
        "publish_result_count": count(publish.get("publish_result_count")),
        "publish_failed_count": count(publish.get("publish_failed_count")),
        "updates_publish_result_count": count(publish.get("updates_publish_result_count")),
        "updates_publish_failed_count": count(publish.get("updates_publish_failed_count")),
        "financial_publish_result_count": count(publish.get("financial_publish_result_count")),
        "financial_publish_failed_count": count(publish.get("financial_publish_failed_count")),
        "financial_publish_enabled": publish.get("financial_publish_enabled") is True,
        "property_review_count": property_review_count,
        "property_skipped_count": property_skipped_count,
        "property_external_excluded_count": property_external_excluded_count,
        "property_excluded_total_count": property_excluded_total_count,
        "guard_workflow_coverage_status": guard_workflow["status"],
        "guard_workflow_digest": guard_workflow["digest"],
        "guard_workflow_update_complete_count": guard_workflow["update_complete_count"],
        "guard_workflow_update_required_count": guard_workflow["update_required_count"],
        "guard_workflow_financial_complete_count": guard_workflow["financial_complete_count"],
        "guard_workflow_financial_required_count": guard_workflow["financial_required_count"],
    }
    review_manifest_effectively_clean = (
        manifest.get("status") == "ok"
        or (
            summary["pending_update_review_count"] == 0
            and summary["pending_financial_review_count"] == 0
            and candidate_packet.get("status") == "ok"
            and (safe_approval_ok or safe_update_reviews_deferred_by_rent_roll)
        )
    )
    summary["review_manifest_effectively_clean"] = review_manifest_effectively_clean
    summary["publish_effectively_verified"] = publish_effectively_verified(publish, summary)
    blockers: list[str] = []
    if not review_manifest_effectively_clean:
        blockers.append(f"review_manifest={manifest.get('status')}:updates={summary['pending_update_review_count']},financials={summary['pending_financial_review_count']}")
    if candidate_packet.get("status") != "ok" or summary["candidate_issue_count"] or summary["candidate_marker_count"] or summary["candidate_financial_gate_issue_count"]:
        blockers.append(
            f"candidate_packet={candidate_packet.get('status')}:issues={summary['candidate_issue_count']},"
            f"markers={summary['candidate_marker_count']},financial_gate={summary['candidate_financial_gate_issue_count']}"
        )
    if safety_scan.get("status") != "ok" or summary["safety_high_count"] or summary["safety_medium_count"] or summary["safety_missing_count"]:
        blockers.append(f"safety_scan={safety_scan.get('status')}:high={summary['safety_high_count']},medium={summary['safety_medium_count']},missing={summary['safety_missing_count']}")
    if summary["guard_issue_count"]:
        blockers.append(f"live_guard_audit_issues={summary['guard_issue_count']}")
    if guard_workflow["status"] != "ok":
        blockers.append(
            "guard_workflow_coverage="
            f"updates={guard_workflow['update_complete_count']}/{guard_workflow['update_required_count']},"
            f"financials={guard_workflow['financial_complete_count']}/{guard_workflow['financial_required_count']}"
        )
    if not summary["guarded_apply_effectively_current"]:
        blockers.append(f"guarded_apply={guarded_apply.get('status')}:apply={guarded_apply.get('apply')}")
    if live_updates.get("status") != "ok" or summary["live_update_check_ok_count"] < summary["live_update_target_count"] or summary["live_update_mismatch_count"]:
        blocker = f"live_updates_verified={summary['live_update_check_ok_count']}/{summary['live_update_target_count']}"
        if summary["live_update_mismatch_count"]:
            blocker += f":mismatches={summary['live_update_mismatch_count']}"
        blockers.append(blocker)
    if live_financials.get("status") != "ok" or summary["live_financial_check_ok_count"] < summary["live_financial_target_count"] or summary["live_financial_mismatch_count"]:
        blocker = f"live_financials_verified={summary['live_financial_check_ok_count']}/{summary['live_financial_target_count']}"
        if summary["live_financial_mismatch_count"]:
            blocker += f":mismatches={summary['live_financial_mismatch_count']}"
        blockers.append(blocker)
    if cdp.get("status") != "ok" or summary["lofty_pm_tab_count"] < 1:
        blockers.append(f"lofty_pm_cdp={cdp.get('status')}:pm_tabs={summary['lofty_pm_tab_count']},login_tabs={summary['lofty_login_tab_count']}")
    if readiness.get("owner_email_allowed") is not True:
        blockers.append(monthly_readiness_blocked_reason(readiness))
    if publish.get("status") != "ok":
        blockers.append(f"publish={publish.get('status')}:issues={publish.get('issue_count')}")
    elif summary["publish_has_apply_evidence"]:
        publish_failure_count = (
            count(publish.get("issue_count"))
            + summary["publish_failed_count"]
            + summary["updates_publish_failed_count"]
            + summary["financial_publish_failed_count"]
        )
        publish_attempt_count = summary["publish_result_count"] + summary["financial_publish_result_count"]
        expected_publish_count = summary["publish_property_count"]
        if not summary["publish_effectively_verified"] and not summary["publish_apply"]:
            blockers.append("publish=ok:apply=false")
        if publish_failure_count:
            blockers.append(f"publish=ok:failed={publish_failure_count}")
        if expected_publish_count and publish_attempt_count < expected_publish_count:
            blockers.append(f"publish=ok:attempted={publish_attempt_count}/{expected_publish_count}")
        if (
            publish.get("apply") is True
            and not publish_failure_count
            and not (expected_publish_count and publish_attempt_count < expected_publish_count)
            and not summary["publish_effectively_verified"]
        ):
            blockers.append("publish=ok:apply=false")

    actionable_summary = owner_gate_actionable_summary(summary, readiness, blockers, property_review_count)
    actions = owner_gate_actions(actionable_summary, summary, readiness, publish)
    primary_blocker, next_action, hold = owner_gate_primary_aliases(actionable_summary)

    digest_inputs = {
        "manifest": {
            "status": manifest.get("status"),
            "pending_update_review_count": manifest.get("pending_update_review_count"),
            "pending_financial_review_count": manifest.get("pending_financial_review_count"),
            "ready_update_count": manifest.get("ready_update_count"),
            "ready_financial_count": manifest.get("ready_financial_count"),
        },
        "candidate_packet": {
            "status": candidate_packet.get("status"),
            "property_count": candidate_packet.get("property_count"),
            "issue_count": candidate_packet.get("issue_count"),
            "marker_count": candidate_packet.get("marker_count"),
            "financial_candidate_gate_issue_count": candidate_packet.get("financial_candidate_gate_issue_count"),
        },
        "guarded": {
            "guard_audit_status": guard_audit.get("status"),
            "guard_issue_count": guard_audit.get("issue_count"),
            "guarded_apply_status": guarded_apply.get("status"),
            "guarded_apply": guarded_apply.get("apply"),
            "guarded_apply_effectively_current": summary["guarded_apply_effectively_current"],
        },
        "live": {
            "updates": {key: live_updates.get(key) for key in ("status", "target_count", "register_count", "check_ok_count", "required_check_ok_count", "unverified_count", "mismatch_count", "target_digest")},
            "financials": {key: live_financials.get(key) for key in ("status", "target_count", "register_count", "check_ok_count", "required_check_ok_count", "unverified_count", "mismatch_count", "target_digest")},
            "cdp": {key: cdp.get(key) for key in ("status", "pm_tab_count", "login_tab_count")},
            "guard_workflow": guard_workflow,
        },
        "email": {
            "owner_email_allowed": readiness.get("owner_email_allowed"),
            "publish_status": publish.get("status"),
            "publish_has_apply_evidence": summary["publish_has_apply_evidence"],
            "publish_apply": publish.get("apply"),
            "publish_effectively_verified": summary["publish_effectively_verified"],
            "publish_result_count": publish.get("publish_result_count"),
            "publish_failed_count": publish.get("publish_failed_count"),
            "financial_publish_result_count": publish.get("financial_publish_result_count"),
            "financial_publish_failed_count": publish.get("financial_publish_failed_count"),
            "send_lock_status": publish.get("send_lock_status"),
            "sent_state_month": publish.get("sent_state_month"),
        },
    }
    checklist_digest = property_checklist_digest(property_checklist)
    digest_inputs["property_checklist"] = {
        "row_count": len(property_checklist),
        "review_count": property_review_count,
        "skipped_count": property_skipped_count,
        "external_excluded_count": property_external_excluded_count,
        "excluded_total_count": property_excluded_total_count,
        "digest": checklist_digest,
        "properties": [record.get("property_name") for record in property_checklist],
    }
    digest = stable_digest(digest_inputs)
    return {
        "status": "ok" if not blockers else "review",
        "generated_at": iso_z(),
        "run_month": summary.get("run_month"),
        "idempotency_key": digest[:16],
        "input_digest": digest,
        "blocker_count": len(blockers),
        "blockers": blockers,
        "primary_blocker": primary_blocker,
        "next_action": next_action,
        "hold": hold,
        "actionable_summary": actionable_summary,
        "summary": summary,
        "actions": actions,
        "property_checklist_count": len(property_checklist),
        "property_review_count": property_review_count,
        "property_skipped_count": property_skipped_count,
        "property_external_excluded_count": property_external_excluded_count,
        "property_excluded_total_count": property_excluded_total_count,
        "property_checklist_digest": checklist_digest,
        "guard_workflow_coverage": guard_workflow,
        "property_checklist": property_checklist,
        "artifacts": {
            "review_manifest": rel(reports / "baselane_financials_monthly_review_manifest.md", root),
            "candidate_packet": rel(reports / "baselane_financials_monthly_review_candidate_packet.md", root),
            "safety_scan": rel(reports / "baselane_financials_monthly_review_safety_scan.md", root),
            "guard_audit": rel(reports / "baselane_financials_monthly_guard_audit.json", root),
            "guarded_apply": rel(reports / "baselane_financials_monthly_guarded_apply.json", root),
            "live_update_capture": rel(reports / "baselane_financials_monthly_live_update_capture.json", root),
            "live_financial_capture": rel(reports / "baselane_financials_monthly_live_financial_capture.json", root),
            "monthly_readiness": rel(reports / "baselane_financials_monthly_readiness.md", root),
            "lofty_pm_publish": rel(reports / "baselane_financials_monthly_lofty_pm_publish.json", root),
            "property_checklist_csv": rel(reports / "baselane_monthly_owner_review_gate.csv", root),
        },
        "review_inputs": digest_inputs,
    }


def csv_value(value: object) -> str:
    if isinstance(value, list):
        return "; ".join(str(item) for item in value)
    if value is None:
        return ""
    return str(value)


def guard_command(record: dict[str, Any], group: str, index: int) -> str:
    guard_commands = record.get("guard_commands") if isinstance(record.get("guard_commands"), dict) else {}
    commands = guard_commands.get(group) if isinstance(guard_commands.get(group), list) else []
    return str(commands[index]) if len(commands) > index else ""


def csv_row(record: dict[str, Any]) -> dict[str, str]:
    row = {field: "" for field in CSV_FIELDS}
    for field in CSV_FIELDS:
        if field in record:
            row[field] = csv_value(record.get(field))
    row["blockers"] = csv_value(record.get("blockers") or [])
    row["next_actions"] = csv_value(record.get("next_actions") or [])
    row["update_guard_artifact_command"] = guard_command(record, "updates", 0)
    row["update_guard_capture_command"] = guard_command(record, "updates", 1)
    row["update_guard_check_command"] = guard_command(record, "updates", 2)
    row["financial_guard_artifact_command"] = guard_command(record, "financials", 0)
    row["financial_guard_capture_command"] = guard_command(record, "financials", 1)
    row["financial_guard_check_command"] = guard_command(record, "financials", 2)
    return row


def property_checklist_digest(property_checklist: list[dict[str, Any]]) -> str:
    normalized_rows = [csv_row(record) for record in property_checklist]
    normalized_rows.sort(key=lambda row: (row.get("status", ""), row.get("property_name", ""), row.get("updates_md", ""), row.get("financials_md", "")))
    return stable_digest({"rows": normalized_rows})


def command_group_complete(commands: list[str], target: object) -> bool:
    target_text = str(target or "").strip()
    if not target_text:
        return False
    joined = "\n".join(commands)
    return (
        len(commands) >= 3
        and "artifact-path" in joined
        and "capture-fetch" in joined
        and "check" in joined
        and target_text in joined
    )


def guard_workflow_coverage(property_checklist: list[dict[str, Any]]) -> dict[str, Any]:
    update_required = []
    update_complete = []
    financial_required = []
    financial_complete = []
    missing: list[dict[str, str]] = []
    normalized_rows = []
    for record in property_checklist:
        if not isinstance(record, dict):
            continue
        status = str(record.get("status") or "")
        if (
            status.startswith("skipped_")
            or status.startswith("excluded_")
            or record.get("external_exclusion") is True
        ):
            continue
        guard_commands = record.get("guard_commands") if isinstance(record.get("guard_commands"), dict) else {}
        update_commands = guard_commands.get("updates") if isinstance(guard_commands.get("updates"), list) else []
        financial_commands = guard_commands.get("financials") if isinstance(guard_commands.get("financials"), list) else []
        property_name = str(record.get("property_name") or "")
        if str(record.get("updates_md") or "").strip():
            update_required.append(property_name)
            if command_group_complete([str(command) for command in update_commands], record.get("live_update_snapshot")):
                update_complete.append(property_name)
            else:
                missing.append({"property_name": property_name, "workflow": "updates"})
        if str(record.get("financials_md") or "").strip():
            financial_required.append(property_name)
            if command_group_complete([str(command) for command in financial_commands], record.get("live_financial_snapshot")):
                financial_complete.append(property_name)
            else:
                missing.append({"property_name": property_name, "workflow": "financials"})
        normalized_rows.append(
            {
                "property_name": property_name,
                "updates_md": record.get("updates_md"),
                "financials_md": record.get("financials_md"),
                "live_update_snapshot": record.get("live_update_snapshot"),
                "live_financial_snapshot": record.get("live_financial_snapshot"),
                "update_commands": [str(command) for command in update_commands],
                "financial_commands": [str(command) for command in financial_commands],
            }
        )
    normalized_rows.sort(key=lambda row: row["property_name"])
    status = "ok" if len(update_required) == len(update_complete) and len(financial_required) == len(financial_complete) else "review"
    return {
        "status": status,
        "digest": stable_digest({"rows": normalized_rows}),
        "update_required_count": len(update_required),
        "update_complete_count": len(update_complete),
        "financial_required_count": len(financial_required),
        "financial_complete_count": len(financial_complete),
        "missing_count": len(missing),
        "missing": missing[:20],
    }


def write_csv(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for record in report.get("property_checklist") or []:
            if isinstance(record, dict):
                writer.writerow(csv_row(record))


def write_markdown(report: dict[str, Any], path: Path) -> None:
    summary = report["summary"]
    actionable = report.get("actionable_summary") if isinstance(report.get("actionable_summary"), dict) else {}
    primary = actionable.get("primary_blocker") if isinstance(actionable.get("primary_blocker"), dict) else {}
    review_records = [record for record in report.get("property_checklist") or [] if record.get("status") != "ok"]
    lines = [
        "# Baselane Monthly Owner Review Gate",
        "",
        f"- Status: `{report['status']}`",
        f"- Blockers: `{report['blocker_count']}`",
        f"- Actionable blockers: `{actionable.get('actionable_blocker_count', report['blocker_count'])}`",
        f"- Idempotency key: `{report['idempotency_key']}`",
        f"- Property checklist digest: `{report.get('property_checklist_digest')}`",
        f"- Run month: `{summary.get('run_month')}`",
        "",
        "## Act Now",
        "",
    ]
    if primary:
        lines.extend(
            [
                f"- Primary blocker: `{primary.get('blocker') or primary.get('class')}`",
                f"- Next action: {primary.get('action')}",
            ]
        )
        if primary.get("artifact"):
            lines.append(f"- Work artifact: `{primary.get('artifact')}`")
        if primary.get("command"):
            lines.append(f"- Rerun command: `{primary.get('command')}`")
        if primary.get("evidence"):
            lines.append(f"- Evidence: {primary.get('evidence')}")
        lines.extend(
            [
                f"- Audit blockers retained in JSON: `{actionable.get('audit_blocker_count', report['blocker_count'])}`",
                f"- Property detail collapsed: `{actionable.get('property_detail_collapsed') is True}`",
            ]
        )
    rendered_actions = list(report["actions"])
    if primary:
        primary_action = str(primary.get("action") or "")
        artifact_action = f"Open {primary.get('artifact')} for the current evidence/work queue." if primary.get("artifact") else ""
        rendered_actions = [action for action in rendered_actions if action not in {primary_action, artifact_action}]
    lines.extend(f"- {action}" for action in rendered_actions)
    lines.extend(
        [
            "",
            "## Counts",
            "",
            f"- Owner approvals pending: updates `{summary['pending_update_review_count']}`; financials `{summary['pending_financial_review_count']}`",
            f"- Review candidates: properties `{summary['candidate_property_count']}`; issues `{summary['candidate_issue_count']}`; markers `{summary['candidate_marker_count']}`; financial gate issues `{summary['candidate_financial_gate_issue_count']}`",
            f"- Safety findings: high `{summary['safety_high_count']}`; medium `{summary['safety_medium_count']}`; missing `{summary['safety_missing_count']}`",
        f"- Guard audit issues: `{summary['guard_issue_count']}`; guarded apply `{summary['guarded_apply']}`",
            f"- Guard workflow coverage: `{summary['guard_workflow_coverage_status']}`; updates `{summary['guard_workflow_update_complete_count']}` / `{summary['guard_workflow_update_required_count']}`; financials `{summary['guard_workflow_financial_complete_count']}` / `{summary['guard_workflow_financial_required_count']}`; digest `{summary['guard_workflow_digest']}`",
            f"- Live update guards registered: `{summary['live_update_registered_count']}` / `{summary['live_update_target_count']}`",
            f"- Live update guards verified: `{summary['live_update_check_ok_count']}` / `{summary['live_update_target_count']}`; mismatches `{summary['live_update_mismatch_count']}`",
            f"- Live financial guards registered: `{summary['live_financial_registered_count']}` / `{summary['live_financial_target_count']}`",
            f"- Live financial guards verified: `{summary['live_financial_check_ok_count']}` / `{summary['live_financial_target_count']}`; mismatches `{summary['live_financial_mismatch_count']}`",
            f"- Lofty CDP: pm tabs `{summary['lofty_pm_tab_count']}`; login tabs `{summary['lofty_login_tab_count']}`",
            f"- Email: allowed `{summary['owner_email_allowed']}`; publish `{summary['publish_status']}`; lock `{summary['owner_email_send_lock_status']}`",
            "",
            "## Per-Property Checklist",
            "",
            f"- Review properties: `{report.get('property_review_count')}` / `{report.get('property_checklist_count')}`",
            f"- Skipped closed properties: `{report.get('property_skipped_count')}`",
            f"- External/manual exclusions: `{report.get('property_external_excluded_count')}`",
            f"- Total excluded from live publish/email: `{report.get('property_excluded_total_count')}`",
        ]
    )
    if review_records:
        if actionable.get("property_detail_collapsed") is True:
            lines.append(
                "- Per-property downstream guard detail is collapsed because the primary portfolio blocker must clear first; use the JSON/CSV artifacts for full audit evidence."
            )
        for record in [] if actionable.get("property_detail_collapsed") is True else review_records:
            guard_commands = record.get("guard_commands") if isinstance(record.get("guard_commands"), dict) else {}
            update_guard_commands = guard_commands.get("updates") or []
            financial_guard_commands = guard_commands.get("financials") or []
            lines.extend(
                [
                    f"- {record.get('property_name')}: `{record.get('status')}`; blockers `{', '.join(record.get('blockers') or [])}`",
                    f"  - Next: `{record.get('next_action_stage')}`; file `{public_tail(record.get('next_action_file'))}`",
                    f"  - Approve update target: `{public_tail(record.get('update_approval_target'))}`",
                    f"  - Approve financial target: `{public_tail(record.get('financial_approval_target'))}`",
                    f"  - Live guards: updates `{record.get('live_update_status')}`; financials `{record.get('live_financial_status')}`",
                ]
            )
            if record.get("next_action_command"):
                lines.append(f"  - Next command: `{record.get('next_action_command')}`")
            if record.get("next_action_detail"):
                lines.append(f"  - Detail: {record.get('next_action_detail')}")
            if update_guard_commands:
                lines.append(f"  - Update guard workflow: `{update_guard_commands[0]}`")
            if financial_guard_commands:
                lines.append(f"  - Financial guard workflow: `{financial_guard_commands[0]}`")
    else:
        lines.append("- No per-property review blockers.")
    lines.extend(
        [
            "",
            "## Artifacts",
            "",
        ]
    )
    for label, artifact in report["artifacts"].items():
        lines.append(f"- {label}: `{artifact}`")
    if report["blockers"]:
        lines.extend(["", "## Blockers", ""])
        lines.extend(f"- `{blocker}`" for blocker in report["blockers"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build one deterministic monthly owner review gate for Lofty PM publish/email readiness.")
    parser.add_argument("--root", type=Path, default=Path(__file__).absolute().parents[1])
    parser.add_argument("--report", type=Path)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--csv", type=Path)
    args = parser.parse_args()

    root = args.root.expanduser().resolve()
    report_path = args.report or root / "reports" / "baselane_monthly_owner_review_gate.json"
    markdown_path = args.markdown or root / "reports" / "baselane_monthly_owner_review_gate.md"
    csv_path = args.csv or root / "reports" / "baselane_monthly_owner_review_gate.csv"
    report = build_report(root)
    report["artifacts"]["property_checklist_csv"] = rel(csv_path, root)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_csv(report, csv_path)
    write_markdown(report, markdown_path)
    print(json.dumps({"status": report["status"], "blocker_count": report["blocker_count"], "idempotency_key": report["idempotency_key"]}, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
