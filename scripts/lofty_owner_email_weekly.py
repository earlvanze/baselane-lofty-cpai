#!/usr/bin/env python3
"""
Weekly owner email pipeline for ECO Systems LLC properties.

Default execution preserves the original publishing behavior. Use `--json` for
a no-send/no-log-write diagnostic for dashboard routing.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

SCRIPT_PATH = Path(__file__).absolute()
DEFAULT_WORKSPACE = SCRIPT_PATH.parents[1]
WORKSPACE = Path(os.environ.get("OPENCLAW_WORKSPACE") or os.environ.get("WORKSPACE_ROOT") or DEFAULT_WORKSPACE)
SKILL_DIR = WORKSPACE / "skills" / "lofty-pm"
PUBLISH_SCRIPT = SKILL_DIR / "scripts" / "publish_latest_update_to_lofty.py"
DEFAULT_MAP = SKILL_DIR / "config" / "property_update_map.json"
LOG_FILE = WORKSPACE / "logs" / "owner_email_weekly.log"
ISSUE_CLASS = "lofty-owner-email-weekly"
DEFAULT_SEND_INTERVAL_DAYS = 7
NATIVE_OWNER_EMAIL_OVERRIDE_ENV = "LOFTY_ALLOW_NATIVE_OWNER_EMAIL_FULL_FIELD_RISK"
NATIVE_OWNER_EMAIL_DISABLED_REASON = (
    "native Lofty owner email disabled: send-property-updates emails the saved full updates field; "
    "use the non-native reviewed email workflow"
)


def diagnostic_command() -> str:
    return f"python3 {SCRIPT_PATH} --json"


DIAGNOSTIC_COMMAND = diagnostic_command()

Logger = Callable[[str], None]


def log(msg: str, log_file: Path = LOG_FILE) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write(f"[{ts}] {msg}\n")
        handle.flush()


def positive_int(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"expected integer days, got {value!r}") from exc
    if parsed < 1:
        raise ValueError("send interval must be at least 1 day")
    return parsed


def default_send_interval_days() -> int:
    raw = os.environ.get("LOFTY_OWNER_UPDATE_SEND_INTERVAL_DAYS")
    if not raw:
        return DEFAULT_SEND_INTERVAL_DAYS
    return positive_int(raw)


def native_owner_email_allowed() -> bool:
    return os.environ.get(NATIVE_OWNER_EMAIL_OVERRIDE_ENV) == "1"


def load_property_map(map_path: Path = DEFAULT_MAP, logger: Logger | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """Load property mapping from lofty-pm config."""
    if not map_path.exists():
        message = f"Property map not found at {map_path}"
        if logger:
            logger(f"ERROR: {message}")
        return [], [message]

    try:
        data = json.loads(map_path.read_text(encoding="utf-8"))
        props = data.get("properties", []) if isinstance(data, dict) else data
        if not isinstance(props, list):
            message = "Property map did not contain a property list"
            if logger:
                logger(f"ERROR: {message}")
            return [], [message]
        return [prop for prop in props if isinstance(prop, dict)], []
    except Exception as exc:  # noqa: BLE001
        message = f"Failed to load property map: {exc}"
        if logger:
            logger(f"ERROR: {message}")
        return [], [message]


def get_pending_properties(props: list[dict[str, Any]], logger: Logger | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """Find properties with pending owner emails."""
    pending: list[dict[str, Any]] = []
    warnings: list[str] = []

    for prop in props:
        updates_md = prop.get("updates_md")
        if not updates_md:
            continue

        state_file = Path(updates_md).parent / ".lofty_publish_state.json"
        if not state_file.exists():
            continue

        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            last_posted_digest = state.get("last_posted_digest")
            last_sent_digest = state.get("last_sent_digest")

            if last_posted_digest and last_posted_digest != last_sent_digest:
                pending.append(
                    {
                        "property_id": prop.get("lofty_property_id"),
                        "property_name": prop.get("property_name"),
                        "updates_md": updates_md,
                        "state_file": str(state_file),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            property_name = prop.get("property_name", "unknown")
            message = f"Failed to read state for {property_name}: {exc}"
            warnings.append(message)
            if logger:
                logger(f"WARNING: {message}")

    return pending, warnings


def remediation_fields(classification: str) -> dict[str, Any]:
    has_issues = classification != "ok"
    return {
        "remediation_class": "operator-reviewed-lofty-owner-email-weekly" if has_issues else "no-remediation-needed",
        "requires_operator_approval": has_issues,
        "requires_interactive_sudo": False,
        "requires_interactive_oauth": False,
        "safe_to_run_automatically": not has_issues,
        "review_command": diagnostic_command(),
        "review_command_safe_to_run_automatically": True,
        "cleanup_command_after_review": None,
        "restart_command_after_review": None,
        "oauth_command_after_review": None,
        "helper_command_after_review": None,
    }


def review_command_validation(command: object | None = None) -> dict[str, Any]:
    command_text = str(command if command is not None else diagnostic_command())
    try:
        parts = shlex.split(command_text)
    except ValueError as exc:
        parts = []
        parse_issue = str(exc)
    else:
        parse_issue = None
    expected_path = str(SCRIPT_PATH)
    script_exists = SCRIPT_PATH.exists()
    script_is_file = SCRIPT_PATH.is_file()
    issues: list[str] = []
    if parse_issue:
        issues.append(f"command parse failed: {parse_issue}")
    if not parts or parts[0] != "python3":
        issues.append("review command must start with python3")
    if expected_path not in parts:
        issues.append(f"review command must target {expected_path}")
    if "--json" not in parts:
        issues.append("review command must include --json")
    if not script_exists:
        issues.append(f"review command script is missing: {expected_path}")
    elif not script_is_file:
        issues.append(f"review command path is not a file: {expected_path}")
    return {
        "command": command_text,
        "expected_script_path": expected_path,
        "script_exists": script_exists,
        "script_is_file": script_is_file,
        "path": expected_path,
        "path_exists": script_exists,
        "python3_present": bool(parts) and parts[0] == "python3",
        "script_path_present": expected_path in parts,
        "json_flag_present": "--json" in parts,
        "requires_executable": False,
        "valid": not issues,
        "issues": issues,
        "issue": issues[0] if issues else None,
    }


def classified_issue_records(issues: list[str], evidence: dict[str, Any], classification: str) -> list[dict[str, Any]]:
    fields = remediation_fields(classification)
    review_validation = review_command_validation(fields.get("review_command"))
    return [
        {
            "issue": issue,
            "issue_class": ISSUE_CLASS,
            "classification": classification,
            "area": "owner-email-weekly",
            "workspace": evidence.get("workspace"),
            "publish_script_exists": evidence.get("publish_script_exists"),
            "property_map_exists": evidence.get("property_map_exists"),
            "property_count": evidence.get("property_count"),
            "pending_property_count": evidence.get("pending_property_count"),
            "state_warning_count": evidence.get("state_warning_count"),
            "review_command_valid": review_validation["valid"],
            "review_command_validation": review_validation,
            **fields,
        }
        for issue in issues
    ]


def classified_issue_summary(report: dict[str, Any]) -> dict[str, Any]:
    classified = report.get("classified_issues") or []
    class_counts: dict[str, int] = {}
    route_counts: dict[str, int] = {}
    for issue in classified:
        issue_class = issue.get("issue_class")
        route = issue.get("classification", report.get("classification"))
        if issue_class:
            class_counts[issue_class] = class_counts.get(issue_class, 0) + 1
        if route:
            route_counts[route] = route_counts.get(route, 0) + 1
    return {
        "total": len(classified),
        "total_count": len(classified),
        "ok_count": int(report.get("ok_count") or 0),
        "issue_count": int(report.get("issue_count") or 0),
        "visible_ok_count": len(report.get("visible_ok") or []),
        "class_counts": class_counts,
        "issue_class_counts": class_counts,
        "route_classification": report.get("classification"),
        "route_classification_counts": route_counts,
        "approval_required_count": sum(1 for issue in classified if issue.get("requires_operator_approval")),
        "review_required_count": int(report.get("review_required_count") or 0),
        "interactive_sudo_count": sum(1 for issue in classified if issue.get("requires_interactive_sudo")),
        "interactive_oauth_count": sum(1 for issue in classified if issue.get("requires_interactive_oauth")),
        "safe_review_command_count": sum(1 for issue in classified if issue.get("review_command_safe_to_run_automatically")),
        "valid_review_command_count": sum(1 for issue in classified if issue.get("review_command_safe_to_run_automatically") and issue.get("review_command_valid")),
        "invalid_review_command_count": sum(1 for issue in classified if issue.get("review_command_safe_to_run_automatically") and not issue.get("review_command_valid")),
        "review_command_validation_issues": [
            validation_issue
            for issue in classified
            if issue.get("review_command_safe_to_run_automatically") and not issue.get("review_command_valid")
            for validation_issue in ((issue.get("review_command_validation") or {}).get("issues") or [])
        ],
        "safe_to_run_automatically": report.get("safe_to_run_automatically") is True,
        "publish_script_exists": report.get("publish_script_exists") is True,
        "property_map_exists": report.get("property_map_exists") is True,
        "log_parent_exists": report.get("log_parent_exists") is True,
        "log_parent_writable": report.get("log_parent_writable") is True,
        "property_count": int(report.get("property_count") or 0),
        "pending_property_count": int(report.get("pending_property_count") or 0),
        "state_file_count": int(report.get("state_file_count") or 0),
        "state_warning_count": int(report.get("state_warning_count") or 0),
        "publish_attempted": report.get("publish_attempted") is True,
        "log_write_attempted": report.get("log_write_attempted") is True,
        "remediation_class": report.get("remediation_class"),
        "cleanup_command_available_after_review": bool(report.get("cleanup_command_after_review")),
        "restart_command_available_after_review": bool(report.get("restart_command_after_review")),
        "oauth_command_available_after_review": bool(report.get("oauth_command_after_review")),
        "helper_command_available_after_review": bool(report.get("helper_command_after_review")),
    }


def build_report(
    workspace: Path = WORKSPACE,
    publish_script: Path | None = None,
    property_map: Path | None = None,
    log_file: Path | None = None,
) -> dict[str, Any]:
    publish_script = publish_script or workspace / "skills" / "lofty-pm" / "scripts" / "publish_latest_update_to_lofty.py"
    property_map = property_map or workspace / "skills" / "lofty-pm" / "config" / "property_update_map.json"
    log_file = log_file or workspace / "logs" / "owner_email_weekly.log"

    issues: list[str] = []
    visible_ok: list[str] = []
    publish_script_exists = publish_script.exists()
    property_map_exists = property_map.exists()
    log_parent = log_file.parent
    log_parent_exists = log_parent.exists()
    log_parent_writable = os.access(log_parent, os.W_OK) if log_parent_exists else False

    props, map_errors = load_property_map(property_map)
    pending, state_warnings = get_pending_properties(props)
    state_file_count = sum(
        1
        for prop in props
        if prop.get("updates_md") and (Path(str(prop["updates_md"])).parent / ".lofty_publish_state.json").exists()
    )

    if not publish_script_exists:
        issues.append(f"Lofty PM publish script not found at {publish_script}")
    issues.extend(map_errors)
    if not props and not map_errors:
        issues.append("No properties found in mapping")
    if state_warnings:
        issues.append(f"{len(state_warnings)} property state files could not be read")
    if not log_parent_exists:
        issues.append(f"log parent does not exist: {log_parent}")
    elif not log_parent_writable:
        issues.append(f"log parent is not writable: {log_parent}")

    if not issues:
        visible_ok.append(
            "OK Lofty owner email weekly config: "
            f"properties={len(props)} pending={len(pending)} state_files={state_file_count}"
        )
        visible_ok.append(
            "OK Lofty owner email weekly diagnostic: "
            "no publish subprocess, owner email send, Lofty update, log write, restart, sudo, OAuth, cleanup, or helper command"
        )

    classification = "lofty-owner-email-weekly-review" if issues else "ok"
    evidence = {
        "workspace": str(workspace),
        "publish_script_exists": publish_script_exists,
        "property_map_exists": property_map_exists,
        "property_count": len(props),
        "pending_property_count": len(pending),
        "state_warning_count": len(state_warnings),
    }
    classified_issues = classified_issue_records(issues, evidence, classification)
    fields = remediation_fields(classification)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "LOFTY_OWNER_EMAIL_WEEKLY_REVIEW" if issues else "NO_REPLY",
        "classification": classification,
        "ok": visible_ok,
        "ok_state": not issues,
        "visible_ok": visible_ok,
        "ok_count": len(visible_ok),
        "issues": issues,
        "issue_count": len(issues),
        "issue_classes": [ISSUE_CLASS] if issues else [],
        "classified_issues": classified_issues,
        "advisory_count": 0,
        "review_required_count": len(classified_issues),
        "workspace": str(workspace),
        "skill_dir": str(workspace / "skills" / "lofty-pm"),
        "publish_script": str(publish_script),
        "publish_script_exists": publish_script_exists,
        "property_map": str(property_map),
        "property_map_exists": property_map_exists,
        "log_file": str(log_file),
        "log_parent_exists": log_parent_exists,
        "log_parent_writable": log_parent_writable,
        "property_count": len(props),
        "pending_property_count": len(pending),
        "send_interval_days": default_send_interval_days(),
        "native_owner_email_allowed": native_owner_email_allowed(),
        "native_owner_email_override_env": NATIVE_OWNER_EMAIL_OVERRIDE_ENV,
        "native_owner_email_disabled_reason": NATIVE_OWNER_EMAIL_DISABLED_REASON,
        "state_file_count": state_file_count,
        "state_warning_count": len(state_warnings),
        "state_warnings": state_warnings[:5],
        "pending_properties_bounded": [
            {
                "property_id_present": bool(prop.get("property_id")),
                "property_name_present": bool(prop.get("property_name")),
                "updates_md_present": bool(prop.get("updates_md")),
                "state_file_present": bool(prop.get("state_file")),
            }
            for prop in pending[:10]
        ],
        "publish_attempted": False,
        "log_write_attempted": False,
        "remediation": {"classification": fields["remediation_class"], **fields},
        **fields,
    }
    report["safe_review_command_count"] = sum(
        1 for issue in classified_issues if issue.get("review_command_safe_to_run_automatically")
    )
    report["valid_review_command_count"] = sum(
        1
        for issue in classified_issues
        if issue.get("review_command_safe_to_run_automatically") and issue.get("review_command_valid")
    )
    report["invalid_review_command_count"] = sum(
        1
        for issue in classified_issues
        if issue.get("review_command_safe_to_run_automatically") and not issue.get("review_command_valid")
    )
    report["review_command_validation_issues"] = [
        validation_issue
        for issue in classified_issues
        if issue.get("review_command_safe_to_run_automatically") and not issue.get("review_command_valid")
        for validation_issue in ((issue.get("review_command_validation") or {}).get("issues") or [])
    ]
    report["classified_issue_summary"] = classified_issue_summary(report)
    return report


def publish_property_updates(
    prop: dict[str, Any],
    publish_script: Path = PUBLISH_SCRIPT,
    dry_run: bool = False,
    send_interval_days: int | None = None,
    logger: Logger | None = None,
) -> bool:
    """Trigger publish_latest_update_to_lofty.py for a single property."""
    if not native_owner_email_allowed():
        if logger:
            logger(f"{prop['property_name']}: skipped native owner email send; {NATIVE_OWNER_EMAIL_DISABLED_REASON}")
        return False
    interval = send_interval_days if send_interval_days is not None else default_send_interval_days()
    cmd = [
        sys.executable,
        str(publish_script),
        "--property",
        prop["updates_md"],
        "--send-interval-days",
        str(interval),
    ]
    if dry_run:
        cmd.append("--dry-run")

    try:
        if logger:
            logger(f"Publishing updates for {prop['property_name']} (ID: {prop['property_id']})")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        if result.returncode == 0:
            if logger:
                logger(f"{prop['property_name']}: Published successfully")
            return True
        if logger:
            logger(f"{prop['property_name']}: Failed with code {result.returncode}")
            if result.stderr:
                logger(f"  Error: {result.stderr[:500]}")
        return False
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger(f"{prop['property_name']}: Exception during publish: {exc}")
        return False


def run_weekly_pipeline(
    workspace: Path = WORKSPACE,
    publish_script: Path | None = None,
    property_map: Path | None = None,
    log_file: Path | None = None,
    dry_run: bool = False,
    send_interval_days: int | None = None,
) -> int:
    publish_script = publish_script or workspace / "skills" / "lofty-pm" / "scripts" / "publish_latest_update_to_lofty.py"
    property_map = property_map or workspace / "skills" / "lofty-pm" / "config" / "property_update_map.json"
    log_file = log_file or workspace / "logs" / "owner_email_weekly.log"
    logger = lambda message: log(message, log_file=log_file)

    logger("=== Weekly owner email pipeline started ===")
    if not publish_script.exists():
        logger(f"ERROR: Lofty PM skill not found at {publish_script}")
        logger("Install skill: https://github.com/earlvanze/lofty-pm")
        return 1

    props, map_errors = load_property_map(property_map, logger=logger)
    if map_errors or not props:
        logger("ERROR: No properties found in mapping")
        return 1

    logger(f"Loaded {len(props)} properties from config")
    pending, _warnings = get_pending_properties(props, logger=logger)
    if not pending:
        logger("No pending owner emails to send")
        return 0

    if not native_owner_email_allowed():
        logger(f"ERROR: {NATIVE_OWNER_EMAIL_DISABLED_REASON}")
        logger(f"Set {NATIVE_OWNER_EMAIL_OVERRIDE_ENV}=1 only for an explicitly approved unsafe native-send test")
        return 2

    logger(f"Found {len(pending)} properties with pending owner updates")
    success_count = 0
    fail_count = 0
    interval = send_interval_days if send_interval_days is not None else default_send_interval_days()
    for prop in pending:
        if publish_property_updates(prop, publish_script=publish_script, dry_run=dry_run, send_interval_days=interval, logger=logger):
            success_count += 1
        else:
            fail_count += 1

    logger(f"Completed: {success_count} succeeded, {fail_count} failed")
    logger("=== Weekly owner email pipeline finished ===")
    return 0 if fail_count == 0 else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run or inspect the weekly Lofty owner-email pipeline")
    parser.add_argument("--json", action="store_true", help="Emit a read-only diagnostic report and do not publish or write logs")
    parser.add_argument("--workspace", default=str(WORKSPACE), help="OpenClaw workspace root")
    parser.add_argument("--publish-script", default=None, help="Override publish script path")
    parser.add_argument("--property-map", default=None, help="Override property map path")
    parser.add_argument("--log-file", default=None, help="Override weekly log path")
    parser.add_argument("--dry-run", action="store_true", help="Pass --dry-run to the publish script on non-JSON runs")
    parser.add_argument("--send-interval-days", type=int, default=None, help="Minimum days between owner email sends per property")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, stdout: TextIO | None = None) -> int:
    args = parse_args(argv)
    workspace = Path(args.workspace)
    publish_script = Path(args.publish_script) if args.publish_script else None
    property_map = Path(args.property_map) if args.property_map else None
    log_file = Path(args.log_file) if args.log_file else None

    if args.json:
        report = build_report(
            workspace=workspace,
            publish_script=publish_script,
            property_map=property_map,
            log_file=log_file,
        )
        print(json.dumps(report, indent=2, sort_keys=True), file=stdout or sys.stdout)
        return 0 if report["status"] == "NO_REPLY" else 1

    return run_weekly_pipeline(
        workspace=workspace,
        publish_script=publish_script,
        property_map=property_map,
        log_file=log_file,
        dry_run=args.dry_run,
        send_interval_days=positive_int(args.send_interval_days) if args.send_interval_days is not None else None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
