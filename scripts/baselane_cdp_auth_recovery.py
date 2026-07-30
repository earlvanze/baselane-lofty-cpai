#!/usr/bin/env python3
"""Read-only Baselane browser-session preflight.

This deliberately never logs in, supplies credentials, solves challenges, or
accesses browser cookies/tokens.  A human must establish the visible Baselane
session before a scheduled workflow can use it.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def endpoint(value: str) -> str:
    value = value.rstrip("/")
    return value[: -len("/json/version")] if value.endswith("/json/version") else value


def tabs(cdp_url: str) -> list[dict]:
    request = urllib.request.Request(f"{endpoint(cdp_url)}/json/list", method="GET")
    with urllib.request.urlopen(request, timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload if isinstance(payload, list) else []


def is_candidate(tab: dict) -> bool:
    url = str(tab.get("url") or "").lower()
    return "app.baselane.com" in url and "/login" not in url and "/signup" not in url


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify that a human-provided Baselane browser session is available.")
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9222/json/version")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--graphql-auth-smoke", action="store_true", help="Accepted for compatibility; no API request is made.")
    parser.add_argument("--recover-login", action="store_true", help="Accepted for compatibility; login recovery is intentionally disabled.")
    parser.add_argument("--recovery-wait-seconds", type=float, default=0.0, help="Accepted for compatibility.")
    parser.add_argument("--handoff", action="store_true", help="Accepted for compatibility.")
    args = parser.parse_args()

    report: dict[str, object] = {
        "generated_at": timestamp(),
        "cdp_url": args.cdp_url,
        "mode": "read_only_human_session_preflight",
        "credential_or_mfa_automation": False,
        "graphql_smoke_performed": False,
        "recover_login_requested": args.recover_login,
    }
    try:
        open_tabs = tabs(args.cdp_url)
        candidates = [tab for tab in open_tabs if is_candidate(tab)]
        report.update(
            {
                "tab_count": len(open_tabs),
                "authenticated_tab_count": len(candidates),
                "verified_authenticated_tab_count": len(candidates),
                "login_tab_count": sum("app.baselane.com/login" in str(tab.get("url") or "").lower() for tab in open_tabs),
                "status": "ok" if candidates else "manual_session_required",
                "manual_auth_required": not bool(candidates),
                "next_action": (
                    "A human must sign in to Baselane in a visible browser and then rerun this workflow."
                    if not candidates
                    else "Continue with the scoped read/preview/apply workflow."
                ),
            }
        )
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        report.update(
            {
                "tab_count": 0,
                "authenticated_tab_count": 0,
                "verified_authenticated_tab_count": 0,
                "login_tab_count": 0,
                "status": "manual_session_required",
                "manual_auth_required": True,
                "error": f"{exc.__class__.__name__}: {exc}"[:240],
                "next_action": "Start an authorized visible browser with remote debugging, sign in manually, and retry.",
            }
        )
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "ok" else 3


if __name__ == "__main__":
    raise SystemExit(main())
