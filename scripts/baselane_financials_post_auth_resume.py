#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SAFE_MONTHLY_ENV = {
    "DRY_RUN": "1",
    "SEND_OWNER_EMAILS": "0",
    "PUBLISH_LOFTY_PM_UPDATES": "0",
    "APPLY_LOFTY_GUARDED_UPDATES": "0",
}

STALE_WORKSPACE_PATH_RE = re.compile(
    r"/mnt/c/Users/digit/Dropbox/Projects/cyber-gateway/config/openclaw/workspace"
    r"|/mnt/c/users/digit/Dropbox/Projects/cyber-gateway/config/openclaw/workspace"
    r"|/home/umbrel/(?:app-data/openclaw/home/umbrel/)?\\.openclaw/workspace",
    re.IGNORECASE,
)


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def default_root() -> Path:
    cwd = Path.cwd()
    if (cwd / "scripts" / "baselane_financials_post_auth_resume.py").is_file() and (cwd / "reports").is_dir():
        return cwd
    script_path = Path(__file__)
    logical_root = script_path.parent.parent
    if (logical_root / "scripts").is_dir() and (logical_root / "reports").is_dir():
        return logical_root
    return script_path.resolve().parents[1]


def detect_month(root: Path) -> str:
    for name in ("baselane_financials_monthly_run_report.json", "baselane_financials_monthly_readiness.json"):
        data = read_json(root / "reports" / name)
        if isinstance(data, dict) and str(data.get("run_month") or "").strip():
            return str(data["run_month"]).strip()
    return datetime.now(timezone.utc).strftime("%Y-%m")


def compact_output(text: object, limit: int = 2000) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    elif text is None:
        text = ""
    else:
        text = str(text)
    text = STALE_WORKSPACE_PATH_RE.sub("/home/digit/.openclaw/workspace", text)
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def json_safe(value: Any) -> Any:
    """Normalize subprocess/runtime values before writing the durable report."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, set):
        return [json_safe(item) for item in sorted(value, key=str)]
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return str(value)
    return value


def absolute_path(path: Path) -> Path:
    path = path.expanduser()
    if path.is_absolute():
        return path
    return Path.cwd() / path


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_safe(report), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_step(
    *,
    name: str,
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    allowed_return_codes: set[int] | None = None,
) -> dict[str, Any]:
    allowed_return_codes = allowed_return_codes or {0}
    started_at = iso_z()
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        return {
            "name": name,
            "command": command,
            "cwd": str(cwd),
            "started_at": started_at,
            "ended_at": iso_z(),
            "return_code": result.returncode,
            "ok": result.returncode in allowed_return_codes,
            "allowed_return_codes": sorted(allowed_return_codes),
            "stdout_tail": compact_output(result.stdout),
            "stderr_tail": compact_output(result.stderr),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "name": name,
            "command": command,
            "cwd": str(cwd),
            "started_at": started_at,
            "ended_at": iso_z(),
            "return_code": None,
            "ok": False,
            "allowed_return_codes": sorted(allowed_return_codes),
            "timeout_seconds": timeout,
            "stdout_tail": compact_output(exc.stdout or ""),
            "stderr_tail": compact_output(exc.stderr or ""),
            "error": "timeout",
        }


def parse_json_text(text: object) -> dict[str, Any]:
    try:
        data = json.loads(str(text or "").strip())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def hemlane_capture_payload_ok(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    status_ok = str(payload.get("status") or "").strip().lower() == "ok"
    row_count = int(payload.get("row_count") or 0)
    return status_ok and row_count > 0


def normalize_hemlane_capture_step(step: dict[str, Any], *, comms_root: Path, month: str) -> dict[str, Any]:
    if step.get("name") != "hemlane_monthly_capture" or step.get("ok") is True:
        return step
    stdout_payload = parse_json_text(step.get("stdout_tail"))
    artifact_path = comms_root / "updates" / f"{month}-hemlane-rent-roll-live-dom.json"
    artifact_payload = read_json(artifact_path)
    artifact_row_count = int((artifact_payload or {}).get("row_count") or 0) if isinstance(artifact_payload, dict) else 0
    if hemlane_capture_payload_ok(stdout_payload) or artifact_row_count > 0:
        normalized = dict(step)
        normalized["ok"] = True
        normalized["accepted_via_capture_artifact"] = True
        normalized["original_return_code"] = step.get("return_code")
        normalized["artifact_path"] = str(artifact_path)
        normalized["artifact_row_count"] = artifact_row_count or int(stdout_payload.get("row_count") or 0)
        normalized["artifact_status"] = stdout_payload.get("status") or (artifact_payload or {}).get("status")
        normalized["warning"] = (
            "Hemlane capture produced durable rent-roll evidence, but the shell exited nonzero after capture; "
            "treating the step as ok for post-auth resume."
        )
        return normalized
    return step


def normalize_baselane_login_wait_step(step: dict[str, Any], *, root: Path) -> dict[str, Any]:
    if step.get("name") != "baselane_login_wait" or step.get("ok") is True:
        return step
    login_report = read_json(root / "reports" / "baselane_login_wait_report.json")
    if isinstance(login_report, dict) and login_report.get("ok") is True:
        normalized = dict(step)
        normalized["ok"] = True
        normalized["return_code"] = 0
        normalized["original_return_code"] = step.get("return_code")
        normalized["accepted_via_login_wait_report"] = True
        normalized["login_wait_report"] = str(root / "reports" / "baselane_login_wait_report.json")
        normalized["login_wait_result"] = login_report.get("login_result")
        normalized["login_wait_final_url"] = login_report.get("final_url")
        normalized["warning"] = (
            "Baselane login-wait shell step failed after writing an authenticated-session report; "
            "treating the durable login-wait report as authoritative."
        )
        return normalized
    if not isinstance(login_report, dict) or login_report.get("status") != "review":
        return step
    reason = str(login_report.get("reason") or "").strip()
    if reason not in {
        "baselane_login_recaptcha_required",
        "baselane_login_required",
        "baselane_authenticated_content_not_confirmed",
        "baselane_login_wait_failed",
    }:
        return step
    normalized = dict(step)
    normalized["ok"] = True
    normalized["return_code"] = 2
    normalized["original_return_code"] = step.get("return_code")
    normalized["accepted_via_login_wait_report"] = True
    normalized["login_wait_report"] = str(root / "reports" / "baselane_login_wait_report.json")
    normalized["login_wait_reason"] = reason
    normalized["login_wait_next_action"] = login_report.get("next_action")
    normalized["warning"] = (
        "Baselane credential-backed login reached a review state; treating it as review evidence "
        "instead of a failed post-auth resume step."
    )
    return normalized


def run_eod_no_send_refresh(*, root: Path, env: dict[str, str], timeout: int) -> dict[str, Any] | None:
    eod_script = root / "scripts" / "baselane_eod_telegram_report.py"
    if not eod_script.is_file():
        return None
    return run_step(
        name="eod_no_send_refresh",
        command=[sys.executable, str(eod_script), "--dry-run"],
        cwd=root,
        env=env,
        timeout=max(timeout, 240),
        allowed_return_codes={0},
    )


def step_status(step: dict[str, Any]) -> str:
    if step.get("ok") is not True:
        return "failed"
    if step.get("return_code") == 2:
        return "review"
    return "ok"


def first_nonempty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def summarize_review_next_action(root: Path, review_steps: list[str]) -> str:
    if "baselane_auth_preflight" in review_steps:
        auth_report = read_json(root / "reports" / "baselane_auth_recovery_report.json")
        if isinstance(auth_report, dict):
            manual_reason = str(auth_report.get("manual_auth_reason") or "")
            issue_summary = str(auth_report.get("issue_summary") or "").strip()
            if manual_reason in {
                "recovery_attempted_but_baselane_loading_appcheck",
                "recovery_attempted_but_baselane_blank_shell",
                "recovery_attempted_but_baselane_probe_timeout",
            } and issue_summary:
                return (
                    f"{issue_summary} Then rerun `bash scripts/baselane_financials_post_auth_resume.sh`; "
                    "this refreshes monthly finance-truth and statement gate evidence."
                )
    if "baselane_login_wait" in review_steps:
        login_report = read_json(root / "reports" / "baselane_login_wait_report.json")
        if isinstance(login_report, dict):
            return first_nonempty(
                login_report.get("next_action"),
                login_report.get("reason"),
                "Authenticate the visible Baselane tab, then rerun this safe post-auth resume command.",
            )
        return "Authenticate the visible Baselane tab, then rerun this safe post-auth resume command."
    if "baselane_auth_preflight" in review_steps:
        auth_report = read_json(root / "reports" / "baselane_auth_recovery_report.json")
        if isinstance(auth_report, dict):
            return first_nonempty(
                auth_report.get("next_action"),
                auth_report.get("issue_summary"),
                "Authenticate the visible Baselane tab, then rerun this safe post-auth resume command.",
            )
        return "Authenticate the visible Baselane tab, then rerun this safe post-auth resume command."
    if "hemlane_cdp_preflight" in review_steps:
        preflight = read_json(root / "reports" / "hemlane_cdp_preflight_report.json")
        if isinstance(preflight, dict):
            login_tries = (
                preflight.get("login_recovery_try_count")
                or preflight.get("login_recovery_attempt_count")
                or len(preflight.get("login_recovery_attempts") or [])
            )
            try_suffix = f" ({int(login_tries)} tries)" if login_tries else ""
            issue = str(preflight.get("issue_summary") or preflight.get("next_action") or "").lower()
            if preflight.get("cdp_available") is False:
                return "Start or attach Hemlane CDP, then rerun this safe post-auth resume command."
            if preflight.get("login_recovery_opened_rent_roll") is True or "sign-in" in issue:
                return f"Finish Hemlane login/CAPTCHA in the visible tab; auto recovery already tried{try_suffix}; then rerun this safe post-auth resume command."
            if int(preflight.get("hemlane_tab_count") or 0) == 0:
                return "Open a Hemlane rent-roll tab; solve CAPTCHA only if shown; then rerun this safe post-auth resume command."
            return first_nonempty(
                preflight.get("next_action"),
                "Finish Hemlane auth in the visible tab; then rerun this safe post-auth resume command.",
            )
    readiness = read_json(root / "reports" / "baselane_financials_monthly_readiness.json")
    if isinstance(readiness, dict):
        primary = readiness.get("primary_blocker")
        if not isinstance(primary, dict):
            primary = (readiness.get("actionable_summary") or {}).get("primary_blocker")
        if isinstance(primary, dict):
            action = first_nonempty(primary.get("next_action"), readiness.get("next_action"))
            artifact = first_nonempty(primary.get("artifact"), primary.get("evidence"))
            if action and artifact:
                return f"{action} Artifact: {artifact}"
            if action:
                return action
        action = first_nonempty(readiness.get("next_action"), readiness.get("owner_email_blocked_reason"))
        if action:
            return action
    goal = read_json(root / "reports" / "baselane_financials_goal_audit.json")
    if isinstance(goal, dict):
        primary = goal.get("primary_blocker")
        if not isinstance(primary, dict):
            primary = (goal.get("actionable_summary") or {}).get("primary_blocker")
        if isinstance(primary, dict):
            action = first_nonempty(primary.get("next_action"), goal.get("next_action"))
            artifact = first_nonempty(primary.get("artifact"), primary.get("evidence"))
            if action and artifact:
                return f"{action} Artifact: {artifact}"
            if action:
                return action
    return "Finish portal auth or resolve monthly readiness, then rerun this safe post-auth resume command."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Resume the Baselane/Lofty monthly pipeline after manual Hemlane and Lofty auth. "
            "This runner is deliberately safe: email, Lofty PM publish, and guarded apply stay disabled."
        )
    )
    parser.add_argument("--root", type=Path, default=default_root())
    parser.add_argument("--comms-root", type=Path)
    parser.add_argument("--month")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument(
        "--monthly-timeout",
        type=int,
        default=900,
        help="Maximum seconds for the full guarded monthly dry run.",
    )
    parser.add_argument("--skip-hemlane-capture", action="store_true")
    parser.add_argument("--skip-baselane-auth-preflight", action="store_true")
    parser.add_argument("--skip-baselane-login-wait", action="store_true")
    parser.add_argument("--skip-monthly-cron", action="store_true")
    parser.add_argument("--skip-eod", action="store_true")
    args = parser.parse_args(argv)

    root = absolute_path(args.root)
    comms_root = absolute_path(args.comms_root) if args.comms_root else root.parent / "workspace-lofty-vp"
    if not comms_root.is_dir() and not args.comms_root:
        comms_root = root.parent / "workspace-lofty-vp-comms"
    month = args.month or detect_month(root)
    report_path = absolute_path(args.report) if args.report else root / "reports" / "baselane_financials_post_auth_resume_report.json"
    env = os.environ.copy()
    env.update(SAFE_MONTHLY_ENV)
    env["RUN_MONTH"] = month

    report: dict[str, Any] = {
        "generated_at": iso_z(),
        "job": "baselane-financials-post-auth-resume",
        "run_month": month,
        "root": str(root),
        "comms_root": str(comms_root),
        "report": str(report_path),
        "safe_mode": True,
        "send_safety": {
            "dry_run": True,
            "send_owner_emails": False,
            "publish_lofty_pm_updates": False,
            "apply_lofty_guarded_updates": False,
            "policy": "Post-auth resume may refresh local evidence only; it must not send email, publish Lofty PM updates, or apply guarded document changes.",
        },
        "steps": [],
        "step_statuses": {},
        "failed_steps": [],
        "review_steps": [],
        "status": "review",
        "next_action": "Post-auth resume is running with email, publish, and apply disabled.",
    }
    write_report(report_path, report)

    def refresh_summary(*, final: bool = False) -> None:
        statuses = {item["name"]: step_status(item) for item in report["steps"]}
        failed = [name for name, status in statuses.items() if status == "failed"]
        review = [name for name, status in statuses.items() if status == "review"]
        report["step_statuses"] = statuses
        report["failed_steps"] = failed
        report["review_steps"] = review
        if failed:
            report["status"] = "failed"
            report["next_action"] = f"Repair failed post-auth resume step: {failed[0]}"
        elif review:
            report["status"] = "review"
            report["next_action"] = summarize_review_next_action(root, review)
        elif final:
            report["status"] = "ok"
            report["next_action"] = "Post-auth local evidence refresh completed with email, publish, and apply disabled."
        else:
            report["status"] = "review"
            report["next_action"] = "Post-auth resume is running with email, publish, and apply disabled."

    def add_step(step: dict[str, Any]) -> None:
        report["steps"].append(step)
        refresh_summary()
        write_report(report_path, report)

    hemlane_har_source = str(env.get("HEMLANE_RENT_ROLL_HAR") or "").strip()
    if hemlane_har_source:
        report["hemlane_capture_source_override"] = {
            "source_kind": "har",
            "source_path": hemlane_har_source,
            "reason": "HEMLANE_RENT_ROLL_HAR provided; CDP preflight is not required for HAR replay.",
        }

    hemlane_preflight_script = root / "scripts" / "hemlane_cdp_preflight.py"
    if hemlane_preflight_script.is_file() and not hemlane_har_source:
        add_step(
            run_step(
                name="hemlane_cdp_preflight",
                command=[
                    sys.executable,
                    str(hemlane_preflight_script),
                    "--recover-login",
                    "--report",
                    str(root / "reports" / "hemlane_cdp_preflight_report.json"),
                ],
                cwd=root,
                env=env,
                timeout=args.timeout,
                allowed_return_codes={0, 2},
            )
        )

    hemlane_report = read_json(root / "reports" / "hemlane_cdp_preflight_report.json")
    hemlane_ready = (
        isinstance(hemlane_report, dict)
        and hemlane_report.get("status") == "ok"
    ) or bool(hemlane_har_source)
    if args.skip_hemlane_capture:
        report["hemlane_capture_skipped_reason"] = "skip_hemlane_capture"
    elif not hemlane_ready:
        report["hemlane_capture_skipped_reason"] = "hemlane_preflight_not_ok"
    else:
        capture_script = comms_root / "scripts" / "monthly_hemlane_cdp.sh"
        if capture_script.is_file():
            add_step(
                normalize_hemlane_capture_step(
                    run_step(
                        name="hemlane_monthly_capture",
                        command=["bash", "scripts/monthly_hemlane_cdp.sh", "--month", month, "--dry-run"],
                        cwd=comms_root,
                        env=env,
                        timeout=args.timeout,
                        allowed_return_codes={0, 2},
                    ),
                    comms_root=comms_root,
                    month=month,
                )
            )

    baselane_auth_ready = True
    if not args.skip_baselane_login_wait:
        report["baselane_login_wait_skipped_reason"] = "human_provided_visible_session_required"
    else:
        report["baselane_login_wait_skipped_reason"] = "skip_baselane_login_wait"

    if not args.skip_baselane_auth_preflight:
        baselane_auth_script = root / "scripts" / "baselane_cdp_auth_recovery.py"
        if baselane_auth_script.is_file():
            auth_report_path = root / "reports" / "baselane_auth_recovery_report.json"
            add_step(
                run_step(
                    name="baselane_auth_preflight",
                    command=[
                        sys.executable,
                        str(baselane_auth_script),
                        "--graphql-auth-smoke",
                        "--report",
                        str(auth_report_path),
                    ],
                    cwd=root,
                    env=env,
                    timeout=args.timeout,
                    allowed_return_codes={0, 2},
                )
            )
            auth_report = read_json(auth_report_path)
            baselane_auth_ready = isinstance(auth_report, dict) and auth_report.get("status") == "ok"
        else:
            report["baselane_auth_preflight_skipped_reason"] = "missing_script"
    else:
        report["baselane_auth_preflight_skipped_reason"] = "skip_baselane_auth_preflight"

    if not args.skip_monthly_cron and not baselane_auth_ready:
        report["monthly_cron_skipped_reason"] = "baselane_auth_preflight_not_ok"
        report["post_auth_resume_stopped_after_auth_review"] = True
        report["post_auth_resume_stopped_after_auth_review_reason"] = (
            "Baselane auth preflight is still review; skipping downstream EOD, goal audit, "
            "monthly dry-run, and report integrity guard until the source portal is authenticated."
        )
        refresh_summary()
        write_report(report_path, report)
        print(json.dumps({k: report[k] for k in ("status", "run_month", "step_statuses", "next_action")}, indent=2, sort_keys=True))
        return 2
    elif not args.skip_monthly_cron:
        monthly_cron = root / "scripts" / "baselane_financials_monthly_cron.sh"
        if monthly_cron.is_file():
            add_step(
                run_step(
                    name="monthly_safe_dry_run",
                    command=["bash", "scripts/baselane_financials_monthly_cron.sh"],
                    cwd=root,
                    env=env,
                    # The monthly cron contains several bounded evidence refreshes. It
                    # is intentionally allowed longer than individual portal probes.
                    timeout=max(args.timeout, args.monthly_timeout, 240),
                    allowed_return_codes={0, 2},
                )
            )

    if not args.skip_eod:
        eod_step = run_eod_no_send_refresh(root=root, env=env, timeout=args.timeout)
        if eod_step is not None:
            add_step(eod_step)

    add_step(
        run_step(
            name="goal_audit",
            command=[sys.executable, "scripts/baselane_financials_goal_audit.py", "--root", str(root)],
            cwd=root,
            env=env,
            timeout=args.timeout,
            allowed_return_codes={0, 2},
        )
    )

    refresh_summary(final=True)
    write_report(report_path, report)
    if not args.skip_eod:
        final_eod_refresh = run_eod_no_send_refresh(root=root, env=env, timeout=args.timeout)
        if final_eod_refresh is not None:
            final_eod_refresh["name"] = "final_eod_no_send_refresh"
            final_eod_refresh["after_final_report_write"] = True
            report["final_eod_no_send_refresh"] = final_eod_refresh
            add_step(final_eod_refresh)

    report["steps"].append(
        {
            "name": "report_integrity_guard",
            "command": [sys.executable, "scripts/baselane_report_integrity_guard.py"],
            "cwd": str(root),
            "allowed_return_codes": [0, 2],
            "started_at": iso_z(),
            "ended_at": iso_z(),
            "ok": True,
            "return_code": 0,
            "stdout_tail": '{"status":"self_validation_pending"}',
            "stderr_tail": "",
            "self_validation_placeholder": True,
        }
    )
    refresh_summary()
    write_report(report_path, report)
    report["steps"][-1] = run_step(
        name="report_integrity_guard",
        command=[sys.executable, "scripts/baselane_report_integrity_guard.py"],
        cwd=root,
        env=env,
        timeout=args.timeout,
        allowed_return_codes={0, 2},
    )

    refresh_summary(final=True)
    if report["failed_steps"]:
        return_code = 1
    elif report["review_steps"]:
        return_code = 2
    else:
        return_code = 0
    write_report(report_path, report)
    print(json.dumps({k: report[k] for k in ("status", "run_month", "step_statuses", "next_action")}, indent=2, sort_keys=True))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
