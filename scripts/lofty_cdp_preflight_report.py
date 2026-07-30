#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROPERTY_OWNERS_URL = "https://www.lofty.ai/property-owners"
MANUAL_AUTH_NEXT_COMMAND = (
    "cd /home/digit/.openclaw/workspace && "
    "DRY_RUN=1 SEND_OWNER_EMAILS=0 PUBLISH_LOFTY_PM_UPDATES=0 APPLY_LOFTY_GUARDED_UPDATES=0 "
    "bash scripts/baselane_financials_monthly_cron.sh"
)


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json_url(base_url: str, path: str, timeout: int) -> tuple[Any | None, str | None]:
    url = base_url.rstrip("/") + path
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace")), None
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return None, str(exc)


def request_json_url(base_url: str, path: str, timeout: int, method: str = "GET") -> tuple[Any | str | None, str | None]:
    url = base_url.rstrip("/") + path
    try:
        request = urllib.request.Request(url, method=method)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            try:
                return json.loads(body), None
            except json.JSONDecodeError:
                return body, None
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        return None, str(exc)


def tab_is_lofty(tab: dict[str, Any]) -> bool:
    url = str(tab.get("url") or "").lower()
    title = str(tab.get("title") or "").lower()
    return "lofty.ai" in url or "lofty ai" in title


def tab_has_pm_evidence(tab: dict[str, Any]) -> bool:
    url = str(tab.get("url") or "").lower()
    title = str(tab.get("title") or "").lower()
    return (
        "/property-owners" in url
        or "/property-owners/edit/" in url
        or "property management" in title
        or "lofty ai" in title and "login" not in url
    )


def tab_is_login(tab: dict[str, Any]) -> bool:
    url = str(tab.get("url") or "").lower()
    title = str(tab.get("title") or "").lower()
    return "lofty.ai/login" in url or "log in" in title or "login" in title


def login_recovery_action(login_tab_count: int, pm_tab_count: int) -> str | None:
    if login_tab_count and not pm_tab_count:
        return (
            "Hard-refresh or close/open Lofty property-owners tab in the dedicated Brave CDP profile; "
            "authenticate only if still redirected, then rerun monthly readiness."
        )
    return None


def next_action_for_state(
    issues: list[str],
    login_tab_count: int,
    pm_tab_count: int,
    recovery_attempts: list[dict[str, Any]],
) -> str:
    if not issues:
        return "Lofty CDP is ready for live capture/publish."
    recovery_opened_property_owners = any(
        attempt.get("method") == "open_property_owners_tab" and attempt.get("ok") is True
        for attempt in recovery_attempts
    )
    if recovery_attempts and recovery_opened_property_owners and login_tab_count and not pm_tab_count:
        return (
            f"Auth Lofty visible tab ({len(recovery_attempts)} tries); "
            "then rerun monthly readiness and live UPDATES.md/FINANCIALS.md guard captures."
        )
    return login_recovery_action(login_tab_count, pm_tab_count) or (
        "Open an authenticated https://www.lofty.ai/property-owners tab in the dedicated Brave CDP profile, then rerun monthly readiness."
    )


def close_tab(base_url: str, tab: dict[str, Any], timeout: int) -> dict[str, Any]:
    tab_id = str(tab.get("id") or "").strip()
    if not tab_id:
        return {"method": "close_login_tab", "ok": False, "error": "missing_tab_id"}
    response, error = request_json_url(base_url, f"/json/close/{urllib.parse.quote(tab_id, safe='')}", timeout)
    return {
        "method": "close_login_tab",
        "tab_id": tab_id,
        "ok": error is None,
        "response": response if isinstance(response, str) else None,
        "error": error,
    }


def open_property_owners_tab(base_url: str, timeout: int) -> dict[str, Any]:
    encoded = urllib.parse.quote(PROPERTY_OWNERS_URL, safe="")
    response, error = request_json_url(base_url, f"/json/new?{encoded}", timeout, method="PUT")
    return {
        "method": "open_property_owners_tab",
        "ok": error is None,
        "opened_url": PROPERTY_OWNERS_URL,
        "target_id": response.get("id") if isinstance(response, dict) else None,
        "url": response.get("url") if isinstance(response, dict) else None,
        "error": error,
    }


def hard_refresh_tab(tab: dict[str, Any], timeout: int) -> dict[str, Any]:
    tab_id = str(tab.get("id") or "").strip()
    ws_url = str(tab.get("webSocketDebuggerUrl") or "").strip()
    if not tab_id:
        return {"method": "hard_refresh_login_tab", "ok": False, "error": "missing_tab_id"}
    if not ws_url:
        return {"method": "hard_refresh_login_tab", "tab_id": tab_id, "ok": False, "error": "missing_websocket_debugger_url"}
    try:
        import websocket  # type: ignore[import-not-found]

        ws = websocket.WebSocket()
        try:
            ws.connect(ws_url, timeout=timeout, origin=None, suppress_origin=True)
            ws.send(json.dumps({"id": 1, "method": "Page.enable", "params": {}}))
            ws.send(json.dumps({"id": 2, "method": "Page.reload", "params": {"ignoreCache": True}}))
            deadline = time.time() + max(float(timeout), 1.0)
            ws.settimeout(max(float(timeout), 1.0))
            page_enable_seen = False
            reload_seen = False
            while time.time() < deadline and not reload_seen:
                message = json.loads(ws.recv())
                if message.get("id") == 1:
                    page_enable_seen = True
                if message.get("id") == 2:
                    reload_seen = True
            return {
                "method": "hard_refresh_login_tab",
                "tab_id": tab_id,
                "ok": reload_seen,
                "page_enable_seen": page_enable_seen,
                "reload_seen": reload_seen,
            }
        finally:
            ws.close()
    except Exception as exc:  # noqa: BLE001
        return {"method": "hard_refresh_login_tab", "tab_id": tab_id, "ok": False, "error": f"{exc.__class__.__name__}: {exc}"[:240]}


def recover_login_tabs(base_url: str, login_tabs: list[dict[str, Any]], timeout: int) -> list[dict[str, Any]]:
    attempts = [hard_refresh_tab(tab, timeout) for tab in login_tabs]
    attempts.extend(close_tab(base_url, tab, timeout) for tab in login_tabs)
    attempts.append(open_property_owners_tab(base_url, timeout))
    return attempts


def recovery_attempt_count(recovery_attempts: list[dict[str, Any]], method: str) -> int:
    return sum(1 for attempt in recovery_attempts if isinstance(attempt, dict) and attempt.get("method") == method)


def report_from_tabs(
    base_url: str,
    version: dict[str, Any] | None,
    tabs: list[dict[str, Any]],
    version_error: str | None,
    tabs_error: str | None,
    recovery_attempts: list[dict[str, Any]] | None = None,
    pre_recovery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tab_rows = tabs if isinstance(tabs, list) else []
    lofty_tabs = [tab for tab in tab_rows if isinstance(tab, dict) and tab_is_lofty(tab)]
    pm_tabs = [tab for tab in lofty_tabs if tab_has_pm_evidence(tab)]
    login_tabs = [tab for tab in lofty_tabs if tab_is_login(tab)]
    issues: list[str] = []
    if version is None:
        issues.append(f"Lofty CDP endpoint unavailable: {base_url} ({version_error})")
    elif not lofty_tabs:
        issues.append("Lofty CDP endpoint is reachable but no Lofty tabs are open")
    elif login_tabs and not pm_tabs:
        opened_property_owners = any(
            attempt.get("method") == "open_property_owners_tab" and attempt.get("ok") is True
            for attempt in (recovery_attempts or [])
            if isinstance(attempt, dict)
        )
        if opened_property_owners:
            issues.append("Lofty CDP endpoint is reachable but recovery still lands on login; authenticate the visible property-owners tab before live capture/publish")
        else:
            issues.append("Lofty CDP endpoint is reachable but the dedicated profile is at Lofty login; hard-refresh or close/open property-owners tab and authenticate only if still redirected before live capture/publish")
    elif not pm_tabs:
        issues.append("Lofty tabs are open but no property-management tab evidence was found")
    issue_summary = "; ".join(issues[:2])
    recovery_attempts = recovery_attempts or []
    login_recovery_opened_property_owners = any(
        attempt.get("method") == "open_property_owners_tab" and attempt.get("ok") is True
        for attempt in recovery_attempts
    )
    login_recovery_hard_refresh_attempted = any(
        attempt.get("method") == "hard_refresh_login_tab"
        for attempt in recovery_attempts
    )
    login_recovery_closed_login_tab_count = recovery_attempt_count(recovery_attempts, "close_login_tab")
    login_recovery_reopened_property_owners_count = recovery_attempt_count(recovery_attempts, "open_property_owners_tab")
    manual_auth_required = bool(
        issues
        and login_tabs
        and not pm_tabs
        and recovery_attempts
        and login_recovery_opened_property_owners
    )
    automated_browser_recovery_complete = bool(recovery_attempts and issues and login_tabs and not pm_tabs)
    report = {
        "generated_at": iso_z(),
        "status": "ok" if not issues else "review",
        "base_url": base_url,
        "cdp_available": version is not None,
        "browser": (version or {}).get("Browser") if isinstance(version, dict) else None,
        "web_socket_debugger_url_present": bool((version or {}).get("webSocketDebuggerUrl")) if isinstance(version, dict) else False,
        "tab_count": len(tab_rows),
        "lofty_tab_count": len(lofty_tabs),
        "pm_tab_count": len(pm_tabs),
        "login_tab_count": len(login_tabs),
        "issues": issues,
        "issue_summary": issue_summary,
        "login_recovery_action": login_recovery_action(len(login_tabs), len(pm_tabs)),
        "login_recovery_attempts": recovery_attempts,
        "login_recovery_attempt_count": len(recovery_attempts),
        "login_recovery_try_count": len(recovery_attempts),
        "login_recovery_exhausted": automated_browser_recovery_complete,
        "login_recovery_performed": bool(recovery_attempts),
        "login_recovery_opened_property_owners": login_recovery_opened_property_owners,
        "login_recovery_hard_refresh_attempted": login_recovery_hard_refresh_attempted,
        "login_recovery_closed_login_tab_count": login_recovery_closed_login_tab_count,
        "login_recovery_reopened_property_owners_count": login_recovery_reopened_property_owners_count,
        "automated_browser_recovery_complete": automated_browser_recovery_complete,
        "manual_auth_required": manual_auth_required,
        "manual_auth_reason": "recovery_opened_property_owners_but_still_at_login" if manual_auth_required else None,
        "manual_auth_phase": "after_browser_recovery" if manual_auth_required else None,
        "manual_auth_portal_url": PROPERTY_OWNERS_URL if manual_auth_required else None,
        "manual_auth_next_command": MANUAL_AUTH_NEXT_COMMAND if manual_auth_required else None,
        "safe_to_retry_after_manual_auth": manual_auth_required,
        "pre_recovery": pre_recovery,
        "next_action": next_action_for_state(issues, len(login_tabs), len(pm_tabs), recovery_attempts),
        "issue_count": len(issues),
        "tabs": [
            {
                "id": tab.get("id"),
                "type": tab.get("type"),
                "title": tab.get("title"),
                "url": tab.get("url"),
                "pm_evidence": tab_has_pm_evidence(tab),
                "login_evidence": tab_is_login(tab),
            }
            for tab in lofty_tabs
        ],
        "version_error": version_error,
        "tabs_error": tabs_error,
    }
    return report


def build_report(base_url: str, timeout: int, recover_login: bool = False, recovery_wait_seconds: float = 2.0) -> dict[str, Any]:
    version, version_error = read_json_url(base_url, "/json/version", timeout)
    tabs, tabs_error = read_json_url(base_url, "/json/list", timeout) if version is not None else (None, None)
    tab_rows = tabs if isinstance(tabs, list) else []
    lofty_tabs = [tab for tab in tab_rows if isinstance(tab, dict) and tab_is_lofty(tab)]
    pm_tabs = [tab for tab in lofty_tabs if tab_has_pm_evidence(tab)]
    login_tabs = [tab for tab in lofty_tabs if tab_is_login(tab)]
    if recover_login and version is not None and login_tabs and not pm_tabs:
        pre_recovery = {"lofty_tab_count": len(lofty_tabs), "pm_tab_count": len(pm_tabs), "login_tab_count": len(login_tabs)}
        recovery_attempts = recover_login_tabs(base_url, login_tabs, timeout)
        if recovery_wait_seconds > 0:
            time.sleep(recovery_wait_seconds)
        tabs, tabs_error = read_json_url(base_url, "/json/list", timeout)
        return report_from_tabs(base_url, version if isinstance(version, dict) else {}, tabs if isinstance(tabs, list) else [], version_error, tabs_error, recovery_attempts, pre_recovery)
    return report_from_tabs(base_url, version if isinstance(version, dict) else None, tab_rows, version_error, tabs_error)


def main() -> int:
    parser = argparse.ArgumentParser(description="Write a safe Lofty CDP preflight report for monthly guard workflows.")
    parser.add_argument("--base-url", default=os.environ.get("LOFTY_CDP_BASE") or "http://127.0.0.1:19222")
    parser.add_argument("--timeout", type=int, default=5)
    parser.set_defaults(recover_login=os.environ.get("LOFTY_CDP_PREFLIGHT_RECOVER_LOGIN", "1") != "0")
    parser.add_argument(
        "--recover-login",
        dest="recover_login",
        action="store_true",
        help="Hard-refresh Lofty login tabs, close them, and open a fresh property-owners tab before reporting. Default.",
    )
    parser.add_argument(
        "--no-recover-login",
        dest="recover_login",
        action="store_false",
        help="Report the current Lofty CDP state without browser recovery.",
    )
    parser.add_argument("--recovery-wait-seconds", type=float, default=2.0)
    parser.add_argument("--report", type=Path, default=Path("reports/lofty_cdp_preflight_report.json"))
    args = parser.parse_args()

    report = build_report(args.base_url, args.timeout, recover_login=args.recover_login, recovery_wait_seconds=args.recovery_wait_seconds)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "base_url", "cdp_available", "lofty_tab_count", "pm_tab_count", "login_tab_count", "issue_count", "issue_summary")}, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
