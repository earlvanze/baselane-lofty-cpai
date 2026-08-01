#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_CDP_URLS = ("http://127.0.0.1:19222", "http://127.0.0.1:9222")
RENT_ROLL_URL = "https://www.hemlane.com/dashboards/owner/reports/rent-roll/"
MANUAL_AUTH_NEXT_COMMAND = (
    "cd /home/digit/.openclaw/workspace-lofty-vp && "
    "bash scripts/monthly_hemlane_cdp.sh --month $(date -u +%Y-%m) --dry-run"
)


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def fetch_json(url: str, timeout: float = 2.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def request_json_url(cdp_url: str, path: str, timeout: float = 3.0, method: str = "GET") -> tuple[Any | str | None, str | None]:
    try:
        request = urllib.request.Request(normalized_cdp_url(cdp_url) + path, method=method)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            try:
                return json.loads(body), None
            except json.JSONDecodeError:
                return body, None
    except Exception as exc:  # noqa: BLE001
        return None, f"{exc.__class__.__name__}: {exc}"[:240]


def normalized_cdp_url(url: str) -> str:
    return url.rstrip("/")


def is_hemlane_url(url: str) -> bool:
    return "hemlane.com" in url.lower()


def is_login_url(url: str) -> bool:
    lower = url.lower()
    return any(part in lower for part in ("/sign-in", "/login", "facebook.com/login", "after_sign_in"))


def is_rent_roll_url(url: str) -> bool:
    lower = url.lower()
    return "/dashboards/owner/reports/rent-roll" in lower


def is_owner_app_url(url: str) -> bool:
    lower = url.lower()
    return any(part in lower for part in ("/property-owners", "/dashboards/owner", "/maintenance"))


def tab_summary(tab: dict[str, Any]) -> dict[str, Any]:
    url = str(tab.get("url") or "")
    return {
        "id": tab.get("id"),
        "title": str(tab.get("title") or "")[:160],
        "url_class": (
            "rent_roll"
            if is_rent_roll_url(url)
            else "owner_app"
            if is_owner_app_url(url)
            else "login"
            if is_login_url(url)
            else "hemlane"
            if is_hemlane_url(url)
            else "other"
        ),
        "url": url[:240],
    }


def hard_refresh_tab(tab: dict[str, Any], timeout: float = 3.0) -> dict[str, Any]:
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


def close_tab(cdp_url: str, tab: dict[str, Any], timeout: float = 3.0) -> dict[str, Any]:
    tab_id = str(tab.get("id") or "").strip()
    if not tab_id:
        return {"method": "close_login_tab", "ok": False, "error": "missing_tab_id"}
    response, error = request_json_url(cdp_url, f"/json/close/{urllib.parse.quote(tab_id, safe='')}", timeout)
    return {
        "method": "close_login_tab",
        "tab_id": tab_id,
        "ok": error is None,
        "response": response if isinstance(response, str) else None,
        "error": error,
    }


def open_rent_roll_tab(cdp_url: str, timeout: float = 3.0) -> dict[str, Any]:
    encoded = urllib.parse.quote(RENT_ROLL_URL, safe="")
    response, error = request_json_url(cdp_url, f"/json/new?{encoded}", timeout, method="PUT")
    return {
        "method": "open_rent_roll_tab",
        "ok": error is None,
        "opened_url": RENT_ROLL_URL,
        "target_id": response.get("id") if isinstance(response, dict) else None,
        "url": response.get("url") if isinstance(response, dict) else None,
        "error": error,
    }


def recover_login_tabs(cdp_url: str, login_tabs: list[dict[str, Any]], timeout: float = 3.0) -> list[dict[str, Any]]:
    attempts = [hard_refresh_tab(tab, timeout) for tab in login_tabs]
    attempts.extend(close_tab(cdp_url, tab, timeout) for tab in login_tabs)
    attempts.append(open_rent_roll_tab(cdp_url, timeout))
    return attempts


def recover_missing_hemlane_tab(cdp_url: str, timeout: float = 3.0) -> list[dict[str, Any]]:
    return [open_rent_roll_tab(cdp_url, timeout)]


def login_recovery_next_action(login_recovery_action: str | None, recovery_attempts: list[dict[str, Any]], logged_in_count: int) -> str | None:
    opened_rent_roll = any(
        attempt.get("method") == "open_rent_roll_tab" and attempt.get("ok") is True
        for attempt in recovery_attempts
    )
    if recovery_attempts and opened_rent_roll and logged_in_count == 0:
        return (
            f"Finish Hemlane login/CAPTCHA in the visible tab; auto hard-refresh/close/open already tried "
            f"({len(recovery_attempts)} steps), "
            "then rerun monthly_hemlane_cdp.sh --dry-run."
        )
    return login_recovery_action


def build_report_from_tabs(
    cdp_url: str,
    version: dict[str, Any] | None,
    tabs: list[dict[str, Any]],
    recovery_attempts: list[dict[str, Any]] | None = None,
    pre_recovery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    hemlane_tabs = [tab for tab in tabs if is_hemlane_url(str(tab.get("url") or ""))]
    login_tabs = [tab for tab in hemlane_tabs if is_login_url(str(tab.get("url") or ""))]
    rent_roll_tabs = [tab for tab in hemlane_tabs if is_rent_roll_url(str(tab.get("url") or "")) and not is_login_url(str(tab.get("url") or ""))]
    owner_tabs = [tab for tab in hemlane_tabs if is_owner_app_url(str(tab.get("url") or "")) and not is_login_url(str(tab.get("url") or ""))]
    logged_in_tabs = [
        tab
        for tab in hemlane_tabs
        if not is_login_url(str(tab.get("url") or "")) and (is_owner_app_url(str(tab.get("url") or "")) or is_rent_roll_url(str(tab.get("url") or "")))
    ]
    status = "ok" if logged_in_tabs else "review"
    issue_summary = None
    login_recovery_action = None
    recovery_attempts = recovery_attempts or []
    login_recovery_opened_rent_roll = any(
        attempt.get("method") == "open_rent_roll_tab" and attempt.get("ok") is True
        for attempt in recovery_attempts
    )
    manual_auth_required = bool(
        status == "review"
        and login_tabs
        and not logged_in_tabs
        and recovery_attempts
        and login_recovery_opened_rent_roll
    )
    automated_browser_recovery_complete = manual_auth_required
    if not hemlane_tabs:
        if login_recovery_opened_rent_roll:
            issue_summary = "Hemlane CDP endpoint is reachable; recovery opened rent-roll but Hemlane still has no visible tab in CDP."
        else:
            issue_summary = "Hemlane CDP endpoint is reachable but no Hemlane tab is open."
    elif not logged_in_tabs:
        if login_recovery_opened_rent_roll:
            issue_summary = "Hemlane CDP endpoint is reachable but recovery already opened rent-roll and still landed on sign-in; authenticate the visible Hemlane tab before capture."
        else:
            issue_summary = "Hemlane CDP endpoint is reachable but Hemlane is at sign-in; authenticate the visible Hemlane tab before capture."
        login_recovery_action = (
            f"Hard refresh the Hemlane sign-in tab or close it and open {RENT_ROLL_URL} "
            "in the dedicated Brave CDP profile; authenticate only if still redirected to sign-in, then rerun monthly_hemlane_cdp.sh --dry-run."
        )
    elif not rent_roll_tabs:
        issue_summary = "Hemlane is authenticated but no rent-roll tab is open; open Owner Reports rent roll before capture."
    return {
        "generated_at": iso_z(),
        "status": status,
        "cdp_available": True,
        "cdp_url": cdp_url,
        "browser": (version or {}).get("Browser"),
        "tab_count": len(tabs),
        "hemlane_tab_count": len(hemlane_tabs),
        "login_tab_count": len(login_tabs),
        "owner_tab_count": len(owner_tabs),
        "rent_roll_tab_count": len(rent_roll_tabs),
        "logged_in_tab_count": len(logged_in_tabs),
        "issue_count": 0 if status == "ok" else 1,
        "issue_summary": issue_summary,
        "login_recovery_action": login_recovery_action,
        "login_recovery_attempts": recovery_attempts,
        "login_recovery_attempt_count": len(recovery_attempts),
        "login_recovery_performed": bool(recovery_attempts),
        "login_recovery_opened_rent_roll": login_recovery_opened_rent_roll,
        "login_recovery_exhausted": automated_browser_recovery_complete,
        "automated_browser_recovery_complete": automated_browser_recovery_complete,
        "manual_auth_required": manual_auth_required,
        "manual_auth_reason": "recovery_opened_rent_roll_but_still_at_login" if manual_auth_required else None,
        "manual_auth_phase": "after_browser_recovery" if manual_auth_required else None,
        "manual_auth_portal_url": RENT_ROLL_URL if manual_auth_required else None,
        "manual_auth_next_command": MANUAL_AUTH_NEXT_COMMAND if manual_auth_required else None,
        "safe_to_retry_after_manual_auth": manual_auth_required,
        "pre_recovery": pre_recovery,
        "next_action": (
            "Hemlane CDP is ready for rent-roll capture."
            if status == "ok"
            else login_recovery_next_action(login_recovery_action, recovery_attempts or [], len(logged_in_tabs))
            or f"Open and authenticate {RENT_ROLL_URL} in the Brave CDP browser, then rerun monthly_hemlane_cdp.sh --dry-run."
        ),
        "tabs": [tab_summary(tab) for tab in hemlane_tabs[:20]],
    }


def build_report(cdp_urls: list[str], recover_login: bool = False, recovery_wait_seconds: float = 2.0) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    for raw_url in cdp_urls:
        cdp_url = normalized_cdp_url(raw_url)
        try:
            version = fetch_json(f"{cdp_url}/json/version")
            tabs = fetch_json(f"{cdp_url}/json/list")
            if not isinstance(tabs, list):
                tabs = []
            if recover_login:
                pre_report = build_report_from_tabs(cdp_url, version if isinstance(version, dict) else {}, tabs)
                login_tabs = [
                    tab
                    for tab in tabs
                    if isinstance(tab, dict)
                    and is_hemlane_url(str(tab.get("url") or ""))
                    and is_login_url(str(tab.get("url") or ""))
                ]
                no_hemlane_tabs = int(pre_report.get("hemlane_tab_count") or 0) == 0
                if pre_report.get("status") == "review" and not pre_report.get("logged_in_tab_count") and (login_tabs or no_hemlane_tabs):
                    if login_tabs:
                        recovery_attempts = recover_login_tabs(cdp_url, login_tabs)
                    else:
                        recovery_attempts = recover_missing_hemlane_tab(cdp_url)
                    if recovery_wait_seconds > 0:
                        time.sleep(recovery_wait_seconds)
                    tabs = fetch_json(f"{cdp_url}/json/list")
                    if not isinstance(tabs, list):
                        tabs = []
                    report = build_report_from_tabs(
                        cdp_url,
                        version if isinstance(version, dict) else {},
                        tabs,
                        recovery_attempts,
                        {
                            "hemlane_tab_count": pre_report.get("hemlane_tab_count"),
                            "login_tab_count": pre_report.get("login_tab_count"),
                            "logged_in_tab_count": pre_report.get("logged_in_tab_count"),
                            "rent_roll_tab_count": pre_report.get("rent_roll_tab_count"),
                        },
                    )
                    report["attempts"] = attempts
                    return report
            report = build_report_from_tabs(cdp_url, version if isinstance(version, dict) else {}, tabs)
            report["attempts"] = attempts
            return report
        except Exception as exc:  # noqa: BLE001
            attempts.append({"cdp_url": cdp_url, "ok": False, "error": f"{exc.__class__.__name__}: {exc}"[:240]})
    return {
        "generated_at": iso_z(),
        "status": "review",
        "cdp_available": False,
        "cdp_url": None,
        "issue_count": 1,
        "issue_summary": "Hemlane CDP endpoint is unavailable; cannot verify auth or capture rent roll.",
        "next_action": f"Start Brave with remote debugging and open {RENT_ROLL_URL}.",
        "attempts": attempts,
        "tabs": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Hemlane CDP preflight for monthly rent-roll capture.")
    parser.add_argument("--cdp-url", action="append", default=[])
    parser.add_argument("--tabs-json", type=Path, help="Test fixture containing a CDP /json/list response.")
    parser.add_argument("--recover-login", action="store_true", help="Hard-refresh Hemlane login tabs, close them, and open a fresh rent-roll tab before reporting.")
    parser.add_argument("--recovery-wait-seconds", type=float, default=2.0)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    if args.tabs_json:
        tabs = read_json(args.tabs_json)
        report = build_report_from_tabs("fixture", {}, tabs if isinstance(tabs, list) else [])
    else:
        report = build_report(args.cdp_url or list(DEFAULT_CDP_URLS), recover_login=args.recover_login, recovery_wait_seconds=args.recovery_wait_seconds)

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report.get(key) for key in ("status", "cdp_available", "hemlane_tab_count", "login_tab_count", "logged_in_tab_count", "rent_roll_tab_count", "login_recovery_performed", "login_recovery_opened_rent_roll", "issue_summary")}, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
