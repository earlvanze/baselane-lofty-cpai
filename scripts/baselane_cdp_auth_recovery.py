#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_CDP_URLS = (
    "http://127.0.0.1:19222",
    "http://host.docker.internal:19222",
    "http://[::1]:19222",
)
DEFAULT_CYBER_TAILNET_IP = "100.115.208.70"
BASELANE_STATEMENTS_URL = "https://app.baselane.com/banking/statements"
POST_AUTH_MONTHLY_RESUME_COMMAND = "bash scripts/baselane_financials_post_auth_resume.sh"
GRAPHQL_AUTH_SMOKE_OPERATION = {
    "operationName": "PropertyList",
    "variables": {},
    "query": "query PropertyList { property { id name address } }",
}


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def normalize_cdp_url(url: str) -> str:
    stripped = url.rstrip("/")
    if stripped.endswith("/json/version"):
        return stripped[: -len("/json/version")]
    return stripped


def _is_loopback_host(host: str | None) -> bool:
    return str(host or "").strip().lower().strip("[]") in {"127.0.0.1", "localhost", "::1"}


def _cdp_request(url: str, method: str = "GET") -> urllib.request.Request:
    request = urllib.request.Request(url, method=method)
    parsed = urllib.parse.urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        port = None
    if not _is_loopback_host(parsed.hostname) or port in {9222, 19222, 19223}:
        request.add_header("Host", "localhost")
    return request


def rewrite_cdp_websocket_url(ws_url: str, cdp_url: str) -> str:
    if not ws_url or not cdp_url:
        return ws_url
    websocket = urllib.parse.urlsplit(ws_url)
    endpoint = urllib.parse.urlsplit(normalize_cdp_url(cdp_url))
    if not _is_loopback_host(websocket.hostname):
        return ws_url
    if _is_loopback_host(endpoint.hostname) and websocket.port and endpoint.port is None:
        return ws_url
    host = endpoint.hostname or websocket.hostname or ""
    host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    port = endpoint.port or websocket.port
    if port is None:
        return ws_url
    netloc = f"{host}:{port}" if port else host
    return urllib.parse.urlunsplit(
        (websocket.scheme, netloc, websocket.path, websocket.query, websocket.fragment)
    )


def cdp_websocket_headers(ws_url: str) -> list[str]:
    try:
        hostname = urllib.parse.urlsplit(ws_url).hostname
    except ValueError:
        hostname = None
    return [] if _is_loopback_host(hostname) else ["Host: localhost"]


def fetch_json(url: str, timeout: float = 8.0) -> Any:
    req = _cdp_request(url)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class OperationDeadlineExceeded(TimeoutError):
    pass


def operation_timeout_seconds(default: float = 6.0) -> float:
    try:
        return max(0.25, float(os.environ.get("BASELANE_CDP_RECOVERY_OPERATION_TIMEOUT_SECONDS") or default))
    except Exception:
        return default


def bounded_operation(callback, fallback: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
    if not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"):
        try:
            return callback()
        except Exception as exc:  # noqa: BLE001
            result = dict(fallback)
            result["error"] = f"{exc.__class__.__name__}: {exc}"[:240]
            return result
    seconds = operation_timeout_seconds(timeout or 6.0)
    previous_handler = signal.getsignal(signal.SIGALRM)

    def handler(_signum, _frame):
        raise OperationDeadlineExceeded(f"operation timed out after {seconds:g}s")

    signal.signal(signal.SIGALRM, handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        return callback()
    except OperationDeadlineExceeded as exc:
        result = dict(fallback)
        result["error"] = f"{exc.__class__.__name__}: {exc}"[:240]
        return result
    except Exception as exc:  # noqa: BLE001
        result = dict(fallback)
        result["error"] = f"{exc.__class__.__name__}: {exc}"[:240]
        return result
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def fetch_json_with_retry(url: str, attempts: int = 2, retry_wait_seconds: float = 0.25) -> Any:
    last_error: Exception | None = None
    for attempt in range(max(attempts, 1)):
        try:
            return fetch_json(url)
        except TimeoutError as exc:
            last_error = exc
            if attempt + 1 < attempts and retry_wait_seconds > 0:
                time.sleep(retry_wait_seconds)
    if last_error is not None:
        raise last_error
    return fetch_json(url)


class BrowserCdp:
    def __init__(self, ws_url: str, timeout: float = 3.0) -> None:
        import websocket  # type: ignore[import-not-found]

        self.websocket_module = websocket
        self.ws = websocket.WebSocket()
        self.ws.connect(
            ws_url,
            timeout=timeout,
            origin=None,
            suppress_origin=True,
            header=cdp_websocket_headers(ws_url),
        )
        self.ws.settimeout(max(float(timeout), 1.0))
        self.timeout = max(float(timeout), 1.0)
        self.next_id = 1

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass

    def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        message: dict[str, Any] = {"id": self.next_id, "method": method}
        if params is not None:
            message["params"] = params
        if session_id:
            message["sessionId"] = session_id
        wanted_id = self.next_id
        self.next_id += 1
        self.ws.send(json.dumps(message))
        deadline = time.time() + max(float(timeout or self.timeout), 1.0)
        while time.time() < deadline:
            self.ws.settimeout(max(0.1, deadline - time.time()))
            try:
                raw = self.ws.recv()
            except (TimeoutError, self.websocket_module.WebSocketTimeoutException):
                if time.time() >= deadline:
                    break
                continue
            reply = json.loads(raw)
            if reply.get("id") != wanted_id:
                continue
            if reply.get("error"):
                raise RuntimeError(json.dumps(reply["error"]))
            return reply.get("result") or {}
        raise TimeoutError(f"CDP {method} timed out")


def browser_cdp(cdp_url: str, timeout: float = 3.0) -> BrowserCdp:
    version = fetch_json(f"{normalize_cdp_url(cdp_url)}/json/version", timeout=timeout)
    if not isinstance(version, dict) or not version.get("webSocketDebuggerUrl"):
        raise RuntimeError("missing_browser_websocket_debugger_url")
    ws_url = rewrite_cdp_websocket_url(str(version["webSocketDebuggerUrl"]), cdp_url)
    return BrowserCdp(ws_url, timeout)


def attach_browser_target(cdp: BrowserCdp, target_id: str, timeout: float = 3.0) -> str:
    result = cdp.send("Target.attachToTarget", {"targetId": target_id, "flatten": True}, timeout=timeout)
    session_id = str(result.get("sessionId") or "")
    if not session_id:
        raise RuntimeError("missing_flattened_session_id")
    return session_id


def request_json_url(cdp_url: str, path: str, timeout: float = 3.0, method: str = "GET") -> tuple[Any | str | None, str | None]:
    try:
        request = _cdp_request(normalize_cdp_url(cdp_url) + path, method=method)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            try:
                return json.loads(body), None
            except json.JSONDecodeError:
                return body, None
    except Exception as exc:  # noqa: BLE001
        return None, f"{exc.__class__.__name__}: {exc}"[:240]


def is_baselane_url(url: str) -> bool:
    return "app.baselane.com" in url.lower()


def is_page_tab(tab: dict[str, Any]) -> bool:
    # Test fixtures historically omit CDP target type; real iframe targets do not.
    target_type = str(tab.get("type") or "page").strip().lower()
    return target_type == "page"


def is_login_url(url: str) -> bool:
    lower = url.lower()
    return any(marker in lower for marker in ("/login", "/session-expired", "/access-denied", "/error"))


def is_statements_url(url: str) -> bool:
    return "/banking/statements" in url.lower()


def is_authenticated_app_url(url: str) -> bool:
    return is_baselane_url(url) and not is_login_url(url)


def is_meaningful_non_baselane_page_url(url: str) -> bool:
    lower = url.lower().strip()
    if not lower.startswith(("http://", "https://")):
        return False
    ignored_prefixes = (
        "http://data/",
        "https://data/",
        "http://newtab/",
        "https://newtab/",
    )
    return not lower.startswith(ignored_prefixes)


def tab_summary(tab: dict[str, Any]) -> dict[str, Any]:
    url = str(tab.get("url") or "")
    return {
        "id": tab.get("id"),
        "type": str(tab.get("type") or "page")[:40],
        "title": str(tab.get("title") or "")[:160],
        "url_class": (
            "statements"
            if is_statements_url(url)
            else "app"
            if is_authenticated_app_url(url)
            else "login"
            if is_login_url(url)
            else "baselane"
            if is_baselane_url(url)
            else "other"
        ),
        "url": url[:240],
    }


def hard_refresh_tab(tab: dict[str, Any], timeout: float = 3.0) -> dict[str, Any]:
    tab_id = str(tab.get("id") or "").strip()
    ws_url = str(tab.get("webSocketDebuggerUrl") or "").strip()
    cdp_url = str(tab.get("_cdp_url") or "").strip()
    url = str(tab.get("url") or "")
    method = "hard_refresh_login_tab" if is_login_url(url) else "hard_refresh_app_tab"
    if not tab_id:
        return {"method": method, "ok": False, "ignore_cache": True, "error": "missing_tab_id"}
    direct_error = None
    if ws_url:
        try:
            import websocket  # type: ignore[import-not-found]

            ws = websocket.WebSocket()
            try:
                rewritten_ws_url = rewrite_cdp_websocket_url(ws_url, cdp_url)
                ws.connect(
                    rewritten_ws_url,
                    timeout=timeout,
                    origin=None,
                    suppress_origin=True,
                    header=cdp_websocket_headers(rewritten_ws_url),
                )
                ws.send(json.dumps({"id": 1, "method": "Page.reload", "params": {"ignoreCache": True}}))
                deadline = time.time() + max(float(timeout), 1.0)
                ws.settimeout(max(float(timeout), 1.0))
                reload_seen = False
                while time.time() < deadline and not reload_seen:
                    message = json.loads(ws.recv())
                    if message.get("id") == 1:
                        reload_seen = True
                return {
                    "method": method,
                    "tab_id": tab_id,
                    "ok": reload_seen,
                    "ignore_cache": True,
                    "transport": "page_websocket",
                    "reload_seen": reload_seen,
                }
            finally:
                ws.close()
        except Exception as exc:  # noqa: BLE001
            direct_error = f"{exc.__class__.__name__}: {exc}"[:240]
    if cdp_url:
        try:
            cdp = browser_cdp(cdp_url, timeout)
            try:
                session_id = attach_browser_target(cdp, tab_id, timeout)
                cdp.send("Page.enable", {}, session_id, timeout)
                cdp.send("Page.reload", {"ignoreCache": True}, session_id, timeout)
                return {
                    "method": method,
                    "tab_id": tab_id,
                    "ok": True,
                    "ignore_cache": True,
                    "transport": "browser_flattened_cdp",
                    "page_enable_seen": True,
                    "reload_seen": True,
                }
            finally:
                cdp.close()
        except Exception as exc:  # noqa: BLE001
            browser_error = f"{exc.__class__.__name__}: {exc}"[:240]
            if direct_error:
                browser_error = f"page_websocket={direct_error}; browser_flattened_cdp={browser_error}"[:240]
            return {"method": method, "tab_id": tab_id, "ok": False, "ignore_cache": True, "error": browser_error}
    return {
        "method": method,
        "tab_id": tab_id,
        "ok": False,
        "ignore_cache": True,
        "error": direct_error or "missing_websocket_debugger_url",
    }


def _classify_auth_probe(tab: dict[str, Any], page_state: dict[str, Any], runtime_enabled: bool, transport: str) -> dict[str, Any]:
    tab_id = str(tab.get("id") or "").strip()
    href = str(page_state.get("href") or tab.get("url") or "")
    title = str(page_state.get("title") or tab.get("title") or "")
    body = str(page_state.get("body") or "")
    lower = f"{href}\n{title}\n{body}".lower()
    body_lower = body.lower()
    authenticated_body_markers = ("transactions", "dashboard", "properties", "account name", "statement period", "download")
    login_visible = (
        is_login_url(href)
        or ("sign in" in lower and "password" in lower)
        or ("log in" in lower and "password" in lower)
    )
    authenticated = (
        is_baselane_url(href)
        and not login_visible
        and any(marker in body_lower for marker in authenticated_body_markers)
    )
    return {
        "tab_id": tab_id,
        "probe_available": bool(page_state),
        "runtime_enabled": runtime_enabled,
        "authenticated": authenticated,
        "login_visible": login_visible,
        "href": href[:240],
        "title": title[:160],
        "body_marker_count": sum(1 for marker in authenticated_body_markers if marker in body_lower),
        "body_excerpt": body[:300],
        "transport": transport,
    }


def inspect_tab_auth_state(tab: dict[str, Any], timeout: float = 3.0) -> dict[str, Any]:
    tab_id = str(tab.get("id") or "").strip()
    ws_url = str(tab.get("webSocketDebuggerUrl") or "").strip()
    cdp_url = str(tab.get("_cdp_url") or "").strip()
    if not tab_id:
        return {"tab_id": None, "probe_available": False, "authenticated": False, "error": "missing_tab_id"}
    expression = (
        "({href: location.href, title: document.title, "
        "body: (document.body && document.body.innerText || '').slice(0, 3000)})"
    )
    if cdp_url:
        try:
            cdp = browser_cdp(cdp_url, timeout)
            try:
                session_id = attach_browser_target(cdp, tab_id, timeout)
                runtime_enabled = False
                try:
                    cdp.send("Runtime.enable", {}, session_id, timeout)
                    runtime_enabled = True
                except Exception:
                    runtime_enabled = False
                result = cdp.send(
                    "Runtime.evaluate",
                    {"expression": expression, "awaitPromise": True, "returnByValue": True},
                    session_id,
                    timeout,
                )
                page_state = ((result.get("result") or {}).get("value") or {})
                return _classify_auth_probe(tab, page_state, runtime_enabled, "browser_flattened_cdp")
            finally:
                cdp.close()
        except Exception as exc:  # noqa: BLE001
            browser_error = f"{exc.__class__.__name__}: {exc}"[:240]
    if not ws_url:
        error = "missing_websocket_debugger_url"
        if cdp_url and browser_error:
            error = f"{error}; browser_flattened_cdp={browser_error}"[:240]
        return {"tab_id": tab_id, "probe_available": False, "authenticated": False, "error": error}
    try:
        import websocket  # type: ignore[import-not-found]

        ws = websocket.WebSocket()
        try:
            ws.connect(ws_url, timeout=timeout, origin=None, suppress_origin=True)
            ws.settimeout(max(float(timeout), 1.0))
            ws.send(json.dumps({"id": 1, "method": "Runtime.enable", "params": {}}))
            ws.send(
                json.dumps(
                    {
                        "id": 2,
                        "method": "Runtime.evaluate",
                        "params": {"expression": expression, "awaitPromise": True, "returnByValue": True},
                    }
                )
            )
            runtime_enabled = False
            page_state: dict[str, Any] = {}
            deadline = time.time() + max(float(timeout), 1.0)
            while time.time() < deadline and not page_state:
                message = json.loads(ws.recv())
                if message.get("id") == 1:
                    runtime_enabled = True
                if message.get("id") == 2:
                    page_state = ((message.get("result") or {}).get("result") or {}).get("value") or {}
            return _classify_auth_probe(tab, page_state, runtime_enabled, "page_websocket")
        finally:
            ws.close()
    except Exception as exc:  # noqa: BLE001
        error = f"{exc.__class__.__name__}: {exc}"
        if cdp_url and browser_error:
            error = f"browser_flattened_cdp={browser_error}; page_websocket={error}"
        return {"tab_id": tab_id, "probe_available": False, "authenticated": False, "error": error[:240]}


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


def close_baselane_app_tab(cdp_url: str, tab: dict[str, Any], timeout: float = 3.0) -> dict[str, Any]:
    tab_id = str(tab.get("id") or "").strip()
    if not tab_id:
        return {"method": "close_baselane_app_tab", "ok": False, "error": "missing_tab_id"}
    response, error = request_json_url(cdp_url, f"/json/close/{urllib.parse.quote(tab_id, safe='')}", timeout)
    return {
        "method": "close_baselane_app_tab",
        "tab_id": tab_id,
        "ok": error is None,
        "response": response if isinstance(response, str) else None,
        "error": error,
    }


def open_statements_tab(cdp_url: str, timeout: float = 3.0) -> dict[str, Any]:
    try:
        cdp = browser_cdp(cdp_url, timeout)
        try:
            response = cdp.send("Target.createTarget", {"url": BASELANE_STATEMENTS_URL}, timeout=timeout)
            return {
                "method": "open_statements_tab",
                "ok": True,
                "opened_url": BASELANE_STATEMENTS_URL,
                "target_id": response.get("targetId"),
                "transport": "browser_flattened_cdp",
                "error": None,
            }
        finally:
            cdp.close()
    except Exception as exc:  # noqa: BLE001
        browser_error = f"{exc.__class__.__name__}: {exc}"[:240]
    encoded = urllib.parse.quote(BASELANE_STATEMENTS_URL, safe="")
    response, error = request_json_url(cdp_url, f"/json/new?{encoded}", timeout, method="PUT")
    if error:
        response, error = request_json_url(cdp_url, f"/json/new?{encoded}", timeout, method="GET")
    return {
        "method": "open_statements_tab",
        "ok": error is None,
        "opened_url": BASELANE_STATEMENTS_URL,
        "target_id": response.get("id") if isinstance(response, dict) else None,
        "url": response.get("url") if isinstance(response, dict) else None,
        "transport": "json_new",
        "error": error,
        "browser_error": browser_error if error else None,
    }


def recover_login_tabs(
    cdp_url: str,
    login_tabs: list[dict[str, Any]],
    baselane_tabs: list[dict[str, Any]] | None = None,
    timeout: float = 3.0,
    max_hard_refresh_tabs: int = 3,
    close_app_tabs_after_refresh: bool = False,
) -> list[dict[str, Any]]:
    login_page_tabs = [tab for tab in login_tabs if is_page_tab(tab)]
    app_page_tabs = [
        tab
        for tab in (baselane_tabs or [])
        if (
            is_page_tab(tab)
            and is_authenticated_app_url(str(tab.get("url") or ""))
            and str(tab.get("id") or "") not in {str(login_tab.get("id") or "") for login_tab in login_page_tabs}
        )
    ]
    refresh_tabs = [*login_page_tabs, *app_page_tabs]
    refresh_tabs = [dict(tab, _cdp_url=cdp_url) for tab in refresh_tabs]
    attempts = [
        bounded_operation(
            lambda tab=tab: hard_refresh_tab(tab, timeout),
            {
                "method": "hard_refresh_login_tab" if is_login_url(str(tab.get("url") or "")) else "hard_refresh_app_tab",
                "tab_id": tab.get("id"),
                "ok": False,
            },
            timeout + 2,
        )
        for tab in refresh_tabs[:max_hard_refresh_tabs]
    ]
    attempts.extend(
        bounded_operation(
            lambda tab=tab: close_tab(cdp_url, tab, timeout),
            {"method": "close_login_tab", "tab_id": tab.get("id"), "ok": False},
            timeout + 2,
        )
        for tab in login_page_tabs
    )
    if close_app_tabs_after_refresh:
        attempts.extend(
            bounded_operation(
                lambda tab=tab: close_baselane_app_tab(cdp_url, tab, timeout),
                {"method": "close_baselane_app_tab", "tab_id": tab.get("id"), "ok": False},
                timeout + 2,
            )
            for tab in app_page_tabs
        )
    attempts.append(
        bounded_operation(
            lambda: open_statements_tab(cdp_url, timeout),
            {"method": "open_statements_tab", "ok": False, "opened_url": BASELANE_STATEMENTS_URL},
            timeout + 4,
        )
    )
    return attempts


def hard_refresh_baselane_app_tabs(
    cdp_url: str,
    baselane_tabs: list[dict[str, Any]],
    timeout: float = 3.0,
    max_tabs: int = 1,
) -> list[dict[str, Any]]:
    refresh_tabs = [
        dict(tab, _cdp_url=cdp_url)
        for tab in baselane_tabs
        if is_page_tab(tab) and is_authenticated_app_url(str(tab.get("url") or ""))
    ]
    return [
        bounded_operation(
            lambda tab=tab: hard_refresh_tab(tab, timeout),
            {
                "method": "hard_refresh_app_tab",
                "tab_id": tab.get("id"),
                "ok": False,
            },
            timeout + 2,
        )
        for tab in refresh_tabs[:max_tabs]
    ]


def content_probe_timed_out(report: dict[str, Any]) -> bool:
    checks = report.get("auth_content_checks")
    if not isinstance(checks, list) or not checks:
        return False
    unavailable = [check for check in checks if isinstance(check, dict) and not check.get("probe_available")]
    if len(unavailable) != len(checks):
        return False
    timeout_markers = ("timed out", "timeout", "websockettimeoutexception")
    return all(any(marker in str(check.get("error") or "").lower() for marker in timeout_markers) for check in unavailable)


def baselane_graphql_auth_smoke(cdp_url: str, target_url: str | None = None, timeout_seconds: float = 45.0) -> dict[str, Any]:
    script = Path("scripts/baselane_graphql_via_cdp.js")
    if not script.is_file():
        return {"ok": False, "error": "missing_baselane_graphql_helper"}
    with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as handle:
        json.dump(GRAPHQL_AUTH_SMOKE_OPERATION, handle)
        payload_path = Path(handle.name)
    try:
        env = os.environ.copy()
        env.update(
            {
                "BASELANE_CDP_VERSION_URL": f"{normalize_cdp_url(cdp_url)}/json/version",
                "BASELANE_GQL_TARGET_URL": target_url or "https://app.baselane.com/transactions",
                "BASELANE_GQL_TIMEOUT_MS": str(int(timeout_seconds * 1000)),
                "BASELANE_GQL_COMMAND_TIMEOUT_MS": str(int(operation_timeout_seconds(12.0) * 1000)),
                "BASELANE_GQL_CREATE_TARGET": "0",
            }
        )
        result = subprocess.run(
            ["node", str(script), str(payload_path)],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(timeout_seconds + 5.0, 10.0),
            env=env,
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        if result.returncode != 0:
            return {
                "ok": False,
                "rc": result.returncode,
                "stderr_excerpt": stderr[-1000:],
                "stdout_excerpt": stdout[:500],
            }
        try:
            response = json.loads(stdout)
        except json.JSONDecodeError as exc:
            return {
                "ok": False,
                "rc": result.returncode,
                "error": f"JSONDecodeError: {exc}"[:240],
                "stdout_excerpt": stdout[:500],
                "stderr_excerpt": stderr[-1000:],
            }
        properties = ((response.get("data") or {}).get("property") or []) if isinstance(response, dict) else []
        errors = response.get("errors") if isinstance(response, dict) else None
        return {
            "ok": bool(properties) and not errors,
            "rc": result.returncode,
            "operationName": GRAPHQL_AUTH_SMOKE_OPERATION["operationName"],
            "property_count": len(properties) if isinstance(properties, list) else 0,
            "errors_present": bool(errors),
            "stderr_excerpt": stderr[-1000:],
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"[:240]}
    finally:
        try:
            payload_path.unlink()
        except Exception:
            pass


def promote_report_with_graphql_smoke(report: dict[str, Any], smoke: dict[str, Any]) -> dict[str, Any]:
    promoted = dict(report)
    promoted["graphql_auth_smoke"] = smoke
    promoted["graphql_auth_smoke_ok"] = smoke.get("ok") is True
    if smoke.get("ok") is True:
        promoted["status"] = "ok"
        promoted["issue_count"] = 0
        promoted["issue_summary"] = None
        promoted["manual_auth_required"] = False
        promoted["manual_auth_reason"] = None
        promoted["manual_auth_phase"] = None
        promoted["manual_auth_portal_url"] = None
        promoted["safe_to_retry_after_manual_auth"] = False
        promoted["auth_proof_source"] = "graphql_propertylist_smoke"
        promoted["next_action"] = (
            f"Baselane GraphQL auth smoke passed; rerun `{POST_AUTH_MONTHLY_RESUME_COMMAND}` "
            "to refresh monthly finance-truth and statement gates."
        )
    return promoted


def maybe_graphql_promote_auth_report(cdp_url: str, tabs: list[dict[str, Any]], report: dict[str, Any]) -> dict[str, Any]:
    if report.get("status") == "ok":
        return report
    if not report.get("url_authenticated_tab_count") or report.get("verified_authenticated_tab_count"):
        return report
    if report.get("login_tab_count"):
        return report
    target_url = next(
        (
            str(tab.get("url") or "")
            for tab in tabs
            if isinstance(tab, dict) and is_page_tab(tab) and is_authenticated_app_url(str(tab.get("url") or ""))
        ),
        "https://app.baselane.com/transactions",
    )
    smoke = bounded_operation(
        lambda: baselane_graphql_auth_smoke(cdp_url, target_url=target_url),
        {"ok": False, "error": "graphql_auth_smoke_timeout"},
        operation_timeout_seconds(55.0),
    )
    return promote_report_with_graphql_smoke(report, smoke)


def content_probe_blank_shell(report: dict[str, Any]) -> bool:
    checks = report.get("auth_content_checks")
    if not isinstance(checks, list) or not checks:
        return False
    for check in checks:
        if not isinstance(check, dict) or check.get("probe_available") is not True:
            continue
        if check.get("authenticated") is True or check.get("login_visible") is True:
            continue
        href = str(check.get("href") or "").lower()
        body_excerpt = str(check.get("body_excerpt") or "").strip()
        marker_count = int(check.get("body_marker_count") or 0)
        if href.startswith("https://app.baselane.com/") and not body_excerpt and marker_count == 0:
            return True
    return False


def content_probe_loading_shell(report: dict[str, Any]) -> bool:
    checks = report.get("auth_content_checks")
    if not isinstance(checks, list) or not checks:
        return False
    loading_markers = {"loading", "loading..."}
    for check in checks:
        if not isinstance(check, dict) or check.get("probe_available") is not True:
            continue
        if check.get("authenticated") is True or check.get("login_visible") is True:
            continue
        href = str(check.get("href") or "").lower()
        body_excerpt = str(check.get("body_excerpt") or "").strip().lower()
        marker_count = int(check.get("body_marker_count") or 0)
        if href.startswith("https://app.baselane.com/") and body_excerpt in loading_markers and marker_count == 0:
            return True
    return False


def renderer_recovery_kind(report: dict[str, Any]) -> str | None:
    if content_probe_timed_out(report):
        return "timeout"
    if content_probe_loading_shell(report):
        return "loading"
    if content_probe_blank_shell(report):
        return "blank"
    return None


def recovery_candidate_priority(candidate: tuple[str, dict[str, Any], list[dict[str, Any]], dict[str, Any]]) -> tuple[int, int]:
    _cdp_url, _version, _tabs, report = candidate
    if report.get("status") == "ok":
        return (0, 0)
    if report.get("statements_tab_count") and report.get("url_authenticated_tab_count"):
        return (1, 0)
    if report.get("url_authenticated_tab_count"):
        return (2, 0)
    if report.get("login_tab_count"):
        return (3, 0)
    if report.get("baselane_tab_count"):
        return (4, 0)
    return (5, int(report.get("non_baselane_page_tab_count") or 0))


def build_report_from_tabs(
    cdp_url: str,
    version: dict[str, Any] | None,
    tabs: list[dict[str, Any]],
    recovery_attempts: list[dict[str, Any]] | None = None,
    pre_recovery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    page_tabs = [tab for tab in tabs if is_page_tab(tab)]
    baselane_tabs = [tab for tab in page_tabs if is_baselane_url(str(tab.get("url") or ""))]
    non_baselane_page_tabs = [
        tab
        for tab in page_tabs
        if is_meaningful_non_baselane_page_url(str(tab.get("url") or ""))
        and not is_baselane_url(str(tab.get("url") or ""))
    ]
    login_tabs = [tab for tab in baselane_tabs if is_login_url(str(tab.get("url") or ""))]
    authed_tabs = [tab for tab in baselane_tabs if is_authenticated_app_url(str(tab.get("url") or ""))]
    loaded_authed_tabs = [tab for tab in authed_tabs if str(tab.get("title") or "").strip()]
    statement_tabs = [tab for tab in authed_tabs if is_statements_url(str(tab.get("url") or ""))]
    auth_content_checks = [
        bounded_operation(
            lambda tab=tab: inspect_tab_auth_state(dict(tab, _cdp_url=cdp_url)),
            {
                "tab_id": tab.get("id"),
                "probe_available": False,
                "authenticated": False,
            },
            6.0,
        )
        for tab in authed_tabs[:3]
    ]
    available_auth_content_checks = [check for check in auth_content_checks if check.get("probe_available")]
    timed_out_auth_content_checks = [
        check
        for check in auth_content_checks
        if (
            not check.get("probe_available")
            and any(marker in str(check.get("error") or "").lower() for marker in ("timed out", "timeout", "websockettimeoutexception"))
        )
    ]
    verified_authed_count = sum(1 for check in available_auth_content_checks if check.get("authenticated") is True)
    recovery_attempts = recovery_attempts or []
    opened_statements = any(attempt.get("method") == "open_statements_tab" and attempt.get("ok") is True for attempt in recovery_attempts)
    hard_refresh_attempts = [
        attempt
        for attempt in recovery_attempts
        if attempt.get("method") in {"hard_refresh_login_tab", "hard_refresh_app_tab"}
    ]
    hard_refresh_login_attempts = [attempt for attempt in recovery_attempts if attempt.get("method") == "hard_refresh_login_tab"]
    hard_refresh_app_attempts = [attempt for attempt in recovery_attempts if attempt.get("method") == "hard_refresh_app_tab"]
    closed_login_tabs = [attempt for attempt in recovery_attempts if attempt.get("method") == "close_login_tab" and attempt.get("ok") is True]
    closed_app_tabs = [attempt for attempt in recovery_attempts if attempt.get("method") == "close_baselane_app_tab" and attempt.get("ok") is True]
    opened_statement_tabs = [attempt for attempt in recovery_attempts if attempt.get("method") == "open_statements_tab" and attempt.get("ok") is True]
    failed_open_statement_tabs = [
        attempt
        for attempt in recovery_attempts
        if attempt.get("method") == "open_statements_tab" and attempt.get("ok") is not True
    ]
    if available_auth_content_checks:
        status = "ok" if verified_authed_count else "review"
    elif auth_content_checks:
        status = "review"
    elif recovery_attempts:
        status = "review"
    else:
        status = "ok" if loaded_authed_tabs else "review"
    manual_auth_required = bool(
        status == "review"
        and recovery_attempts
        and (opened_statements or failed_open_statement_tabs or closed_app_tabs)
    )
    recovery_still_loading = bool(manual_auth_required and content_probe_loading_shell({"auth_content_checks": auth_content_checks}))
    recovery_still_blank = bool(manual_auth_required and content_probe_blank_shell({"auth_content_checks": auth_content_checks}))
    recovery_probe_timeout = bool(manual_auth_required and content_probe_timed_out({"auth_content_checks": auth_content_checks}))
    if not baselane_tabs:
        issue_summary = "Baselane CDP endpoint is reachable but no Baselane tab is open."
    elif recovery_still_loading:
        issue_summary = "Baselane browser recovery reopened statements but the app shell is still stuck on Loading/appcheck; hard-refresh or complete the visible challenge."
    elif recovery_still_blank:
        issue_summary = "Baselane browser recovery reopened statements but the app shell is still blank; hard-refresh or close/open the visible Baselane tab."
    elif recovery_probe_timeout:
        issue_summary = "Baselane browser recovery reopened statements but content probing still times out; hard-refresh the visible Baselane tab."
    elif manual_auth_required:
        issue_summary = "Baselane browser recovery already reopened statements and still landed on login; authenticate the visible Baselane tab before capture."
    elif available_auth_content_checks and not verified_authed_count:
        issue_summary = "Baselane tab URL looks authenticated, but page content did not verify an authenticated Baselane session."
    elif auth_content_checks and not available_auth_content_checks:
        issue_summary = "Baselane tab URL/title looks authenticated, but every page content probe timed out."
    elif not authed_tabs:
        issue_summary = "Baselane is at login; authenticate the visible Baselane tab before statement capture."
    elif not statement_tabs:
        issue_summary = "Baselane appears authenticated; rerun statement capture so it can navigate to bank statements."
    else:
        issue_summary = None
    return {
        "generated_at": iso_z(),
        "status": status,
        "cdp_available": True,
        "cdp_url": cdp_url,
        "browser": (version or {}).get("Browser"),
        "tab_count": len(tabs),
        "baselane_tab_count": len(baselane_tabs),
        "non_baselane_page_tab_count": len(non_baselane_page_tabs),
        "login_tab_count": len(login_tabs),
        "url_authenticated_tab_count": len(authed_tabs),
        "authenticated_tab_count": verified_authed_count if available_auth_content_checks else len(loaded_authed_tabs),
        "loaded_authenticated_tab_count": len(loaded_authed_tabs),
        "auth_content_probe_count": len(available_auth_content_checks),
        "auth_content_probe_timeout_count": len(timed_out_auth_content_checks),
        "verified_authenticated_tab_count": verified_authed_count,
        "auth_content_checks": auth_content_checks,
        "statements_tab_count": len(statement_tabs),
        "issue_count": 0 if status == "ok" else 1,
        "issue_summary": issue_summary,
        "login_recovery_attempts": recovery_attempts,
        "login_recovery_attempt_count": len(recovery_attempts),
        "login_recovery_performed": bool(recovery_attempts),
        "login_recovery_hard_refresh_attempted": bool(hard_refresh_attempts),
        "login_recovery_hard_refresh_attempt_count": len(hard_refresh_attempts),
        "login_recovery_hard_refresh_login_tab_count": len(hard_refresh_login_attempts),
        "login_recovery_hard_refresh_app_tab_count": len(hard_refresh_app_attempts),
        "login_recovery_closed_login_tab_count": len(closed_login_tabs),
        "login_recovery_closed_app_tab_count": len(closed_app_tabs),
        "login_recovery_opened_statement_tab_count": len(opened_statement_tabs),
        "login_recovery_failed_open_statement_tab_count": len(failed_open_statement_tabs),
        "login_recovery_opened_statements": opened_statements,
        "login_recovery_exhausted": manual_auth_required,
        "automated_browser_recovery_complete": manual_auth_required,
        "manual_auth_required": manual_auth_required,
        "manual_auth_reason": (
            "recovery_attempted_but_baselane_loading_appcheck"
            if recovery_still_loading
            else "recovery_attempted_but_baselane_blank_shell"
            if recovery_still_blank
            else "recovery_attempted_but_baselane_probe_timeout"
            if recovery_probe_timeout
            else "recovery_attempted_but_baselane_not_verified"
            if manual_auth_required
            else None
        ),
        "manual_auth_phase": "after_browser_recovery" if manual_auth_required else None,
        "manual_auth_portal_url": BASELANE_STATEMENTS_URL if manual_auth_required else None,
        "safe_to_retry_after_manual_auth": manual_auth_required,
        "pre_recovery": pre_recovery,
        "next_action": (
            f"Baselane CDP is authenticated; rerun `{POST_AUTH_MONTHLY_RESUME_COMMAND}` to refresh monthly finance-truth and statement gates."
            if status == "ok"
            else f"Auth Baselane visible tab, then rerun `{POST_AUTH_MONTHLY_RESUME_COMMAND}`; this refreshes monthly finance-truth and statement gate evidence."
            if manual_auth_required
            else f"Open and authenticate {BASELANE_STATEMENTS_URL} in the Brave CDP browser, then rerun `{POST_AUTH_MONTHLY_RESUME_COMMAND}`."
        ),
        "tabs": [tab_summary(tab) for tab in baselane_tabs[:20]],
    }


def build_report(
    cdp_urls: list[str],
    recover_login: bool = False,
    recovery_wait_seconds: float = 2.0,
    graphql_auth_smoke: bool = False,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    review_candidates: list[tuple[str, dict[str, Any], list[dict[str, Any]], dict[str, Any]]] = []
    for raw_url in cdp_urls:
        cdp_url = normalize_cdp_url(raw_url)
        try:
            version = fetch_json_with_retry(f"{cdp_url}/json/version")
            tabs = fetch_json_with_retry(f"{cdp_url}/json/list")
            if not isinstance(tabs, list):
                tabs = []
            report = build_report_from_tabs(cdp_url, version if isinstance(version, dict) else {}, tabs)
            if graphql_auth_smoke:
                report = maybe_graphql_promote_auth_report(cdp_url, tabs, report)
            if report.get("status") == "ok":
                report["attempts"] = attempts
                return report
            review_candidates.append((cdp_url, version if isinstance(version, dict) else {}, tabs, report))
        except Exception as exc:  # noqa: BLE001
            attempts.append({"cdp_url": cdp_url, "ok": False, "error": f"{exc.__class__.__name__}: {exc}"[:240]})
    if recover_login:
        for cdp_url, version, tabs, pre_report in sorted(review_candidates, key=recovery_candidate_priority):
            login_tabs = [
                tab
                for tab in tabs
                if isinstance(tab, dict)
                and is_baselane_url(str(tab.get("url") or ""))
                and is_login_url(str(tab.get("url") or ""))
            ]
            should_recover_login = (
                pre_report.get("status") == "review"
                and not pre_report.get("verified_authenticated_tab_count")
                and (
                    login_tabs
                    or (
                        pre_report.get("url_authenticated_tab_count")
                        and not pre_report.get("non_baselane_page_tab_count")
                    )
                    or (
                        not pre_report.get("baselane_tab_count")
                        and not pre_report.get("non_baselane_page_tab_count")
                    )
                )
            )
            recovery_kind = renderer_recovery_kind(pre_report)
            should_recover_renderer_timeout = (
                pre_report.get("status") == "review"
                and pre_report.get("baselane_tab_count")
                and pre_report.get("url_authenticated_tab_count")
                and not pre_report.get("verified_authenticated_tab_count")
                and (
                    recovery_kind == "timeout"
                    or recovery_kind == "loading"
                    or (recovery_kind == "blank" and not pre_report.get("non_baselane_page_tab_count"))
                )
            )
            if not should_recover_login and not should_recover_renderer_timeout:
                continue
            baselane_tabs = [
                tab
                for tab in tabs
                if isinstance(tab, dict) and is_baselane_url(str(tab.get("url") or ""))
            ]
            pre_recovery = {
                "baselane_tab_count": pre_report.get("baselane_tab_count"),
                "login_tab_count": pre_report.get("login_tab_count"),
                "authenticated_tab_count": pre_report.get("authenticated_tab_count"),
                "url_authenticated_tab_count": pre_report.get("url_authenticated_tab_count"),
                "verified_authenticated_tab_count": pre_report.get("verified_authenticated_tab_count"),
                "statements_tab_count": pre_report.get("statements_tab_count"),
                "auth_content_probe_timeout_count": pre_report.get("auth_content_probe_timeout_count"),
                "renderer_timeout_recovery": should_recover_renderer_timeout,
                "renderer_blank_shell_recovery": content_probe_blank_shell(pre_report),
                "renderer_loading_shell_recovery": content_probe_loading_shell(pre_report),
            }
            recovery_attempts: list[dict[str, Any]] = []
            if should_recover_renderer_timeout and recovery_kind in {"loading", "blank"}:
                recovery_attempts.extend(
                    hard_refresh_baselane_app_tabs(
                        cdp_url,
                        baselane_tabs,
                        timeout=operation_timeout_seconds(8.0),
                        max_tabs=1,
                    )
                )
                pre_recovery["auto_hard_refresh_before_close_open_attempted"] = bool(recovery_attempts)
                pre_recovery["auto_hard_refresh_before_close_open_attempt_count"] = len(recovery_attempts)
                if recovery_wait_seconds > 0:
                    time.sleep(recovery_wait_seconds)
                try:
                    tabs = fetch_json_with_retry(f"{cdp_url}/json/list")
                    if not isinstance(tabs, list):
                        tabs = []
                except Exception as exc:  # noqa: BLE001
                    attempts.append(
                        {
                            "cdp_url": cdp_url,
                            "ok": False,
                            "stage": "post_hard_refresh_tab_fetch",
                            "error": f"{exc.__class__.__name__}: {exc}"[:240],
                        }
                    )
                    report = build_report_from_tabs(cdp_url, version, tabs, recovery_attempts, pre_recovery)
                    report["post_hard_refresh_tab_fetch_failed"] = True
                    report["post_hard_refresh_tab_fetch_error"] = f"{exc.__class__.__name__}: {exc}"[:240]
                    report["attempts"] = attempts
                    return report
                hard_refresh_report = build_report_from_tabs(
                    cdp_url,
                    version,
                    tabs,
                    recovery_attempts,
                    pre_recovery,
                )
                if graphql_auth_smoke:
                    hard_refresh_report = maybe_graphql_promote_auth_report(cdp_url, tabs, hard_refresh_report)
                if hard_refresh_report.get("status") == "ok":
                    hard_refresh_report["attempts"] = attempts
                    return hard_refresh_report
                login_tabs = [
                    tab
                    for tab in tabs
                    if isinstance(tab, dict)
                    and is_baselane_url(str(tab.get("url") or ""))
                    and is_login_url(str(tab.get("url") or ""))
                ]
                baselane_tabs = [
                    tab
                    for tab in tabs
                    if isinstance(tab, dict) and is_baselane_url(str(tab.get("url") or ""))
                ]
            else:
                pre_recovery["auto_hard_refresh_before_close_open_attempted"] = False
                pre_recovery["auto_hard_refresh_before_close_open_attempt_count"] = 0
            recovery_attempts.extend(
                recover_login_tabs(
                    cdp_url,
                    login_tabs,
                    baselane_tabs,
                    timeout=operation_timeout_seconds(8.0),
                    close_app_tabs_after_refresh=bool(should_recover_renderer_timeout),
                    max_hard_refresh_tabs=0 if should_recover_renderer_timeout else 3,
                )
            )
            if recovery_wait_seconds > 0:
                time.sleep(recovery_wait_seconds)
            try:
                tabs = fetch_json_with_retry(f"{cdp_url}/json/list")
                if not isinstance(tabs, list):
                    tabs = []
            except Exception as exc:  # noqa: BLE001
                attempts.append(
                    {
                        "cdp_url": cdp_url,
                        "ok": False,
                        "stage": "post_recovery_tab_fetch",
                        "error": f"{exc.__class__.__name__}: {exc}"[:240],
                    }
                )
                report = build_report_from_tabs(cdp_url, version, tabs, recovery_attempts, pre_recovery)
                report["post_recovery_tab_fetch_failed"] = True
                report["post_recovery_tab_fetch_error"] = f"{exc.__class__.__name__}: {exc}"[:240]
                report["attempts"] = attempts
                return report
            report = build_report_from_tabs(
                cdp_url,
                version,
                tabs,
                recovery_attempts,
                pre_recovery,
            )
            if graphql_auth_smoke:
                report = maybe_graphql_promote_auth_report(cdp_url, tabs, report)
            report["attempts"] = attempts
            return report
    if review_candidates:
        report = review_candidates[0][3]
        report["attempts"] = attempts
        if (
            attempts
            and not report.get("baselane_tab_count")
            and report.get("non_baselane_page_tab_count")
            and not report.get("login_recovery_performed")
        ):
            report["issue_summary"] = (
                "Preferred Baselane CDP endpoint failed, and the reachable CDP browser is a shared "
                "non-Baselane session with no Baselane tab."
            )
            report["next_action"] = (
                "Recover or restart the Baselane CDP browser/port, then open and authenticate "
                f"{BASELANE_STATEMENTS_URL}; do not use the shared Hemlane/Lofty CDP session for Baselane."
            )
            report["preferred_cdp_attempt_failed"] = True
        return report
    return {
        "generated_at": iso_z(),
        "status": "review",
        "cdp_available": False,
        "cdp_url": None,
        "issue_count": 1,
        "issue_summary": "Baselane CDP endpoint is unavailable; cannot verify auth or recover the statement tab.",
        "next_action": f"Start Brave with remote debugging and open {BASELANE_STATEMENTS_URL}.",
        "attempts": attempts,
        "login_recovery_attempts": [],
        "login_recovery_attempt_count": 0,
        "login_recovery_performed": False,
        "tabs": [],
    }


def cdp_urls_from_env(raw: str | None) -> list[str]:
    urls: list[str] = []
    if raw:
        urls.append(normalize_cdp_url(raw))
    urls.extend(DEFAULT_CDP_URLS)

    cyber_ip = os.environ.get("BASELANE_CDP_TAILNET_IP", "").strip()
    if not cyber_ip:
        try:
            cyber_ip = subprocess.run(
                ["tailscale", "ip", "--4", "cyber"],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=2,
            ).stdout.strip()
        except Exception:
            cyber_ip = ""
    cyber_ip = cyber_ip or DEFAULT_CYBER_TAILNET_IP
    urls.append(f"http://{cyber_ip}:19222")

    seen: set[str] = set()
    deduped: list[str] = []
    for url in urls:
        normalized = normalize_cdp_url(url)
        if normalized and normalized not in seen:
            seen.add(normalized)
            deduped.append(normalized)
    return deduped


def small_model_handoff(report: dict[str, Any]) -> dict[str, Any]:
    """Return one safe, deterministic next step for an auth-recovery agent.

    This recovery probe does not enter credentials itself.  When ordinary
    Baselane login is required, it delegates to the visible-browser session
    seeder, which retrieves credentials from Bitwarden at runtime.  CAPTCHA,
    MFA, bot-detection, and payment-confirmation challenges are left open.
    """
    status = str(report.get("status") or "review")
    if status == "ok":
        return {
            "status": "ready",
            "manual_action_required": False,
            "next_action": "Run the safe post-auth monthly resume.",
            "next_command": "bash scripts/baselane_financials_post_auth_resume.sh",
        }

    if report.get("cdp_available") is not True:
        return {
            "status": "cdp_unavailable",
            "manual_action_required": True,
            "next_action": (
                "Open Brave with remote debugging and navigate the visible Baselane tab to "
                f"{BASELANE_STATEMENTS_URL}."
            ),
            "next_command": "python3 scripts/baselane_cdp_auth_recovery.py --graphql-auth-smoke --handoff",
        }

    if int(report.get("login_tab_count") or 0) > 0 or report.get("manual_auth_required") is True:
        return {
            "status": "visible_login_required",
            "manual_action_required": False,
            "next_action": (
                "Run the Bitwarden-backed visible-browser session seeder. It may submit ordinary "
                "username/password credentials; leave any CAPTCHA, MFA, bot-detection, or "
                "payment-confirmation challenge open."
            ),
            "next_command": "bash scripts/baselane_seed_session.sh",
            "post_seed_command": (
                "python3 scripts/baselane_cdp_auth_recovery.py --graphql-auth-smoke --handoff"
            ),
        }

    return {
        "status": "browser_recovery_required",
        "manual_action_required": False,
        "next_action": "Prepare a fresh visible Baselane statements tab, then verify auth again.",
        "next_command": (
            "python3 scripts/baselane_cdp_auth_recovery.py --recover-login "
            "--graphql-auth-smoke --handoff"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Safe Baselane CDP auth recovery for monthly statement gates.")
    parser.add_argument("--cdp-url", action="append", default=[])
    parser.add_argument("--tabs-json", type=Path, help="Test fixture containing a CDP /json/list response.")
    parser.add_argument("--recover-login", action="store_true", help="Hard-refresh Baselane login tabs, close them, and open a fresh statements tab before reporting.")
    parser.add_argument(
        "--graphql-auth-smoke",
        action="store_true",
        default=os.environ.get("BASELANE_GRAPHQL_AUTH_SMOKE") == "1",
        help="Accept a successful read-only Baselane GraphQL PropertyList call as auth proof when DOM probes time out.",
    )
    parser.add_argument("--recovery-wait-seconds", type=float, default=2.0)
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--handoff",
        action="store_true",
        help="Emit a compact, single-action auth handoff for deterministic local models.",
    )
    args = parser.parse_args()

    if args.tabs_json:
        tabs = read_json(args.tabs_json)
        report = build_report_from_tabs("fixture", {}, tabs if isinstance(tabs, list) else [])
    else:
        env_url = os.environ.get("BASELANE_CDP_VERSION_URL") or os.environ.get("BASELANE_CDP_BASE")
        report = build_report(
            args.cdp_url or cdp_urls_from_env(env_url),
            recover_login=args.recover_login,
            recovery_wait_seconds=args.recovery_wait_seconds,
            graphql_auth_smoke=args.graphql_auth_smoke,
        )

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.handoff:
        print(json.dumps(small_model_handoff(report), indent=2, sort_keys=True))
    else:
        print(
            json.dumps(
                {
                    key: report.get(key)
                    for key in (
                        "status",
                        "cdp_available",
                        "baselane_tab_count",
                        "login_tab_count",
                        "authenticated_tab_count",
                        "url_authenticated_tab_count",
                        "verified_authenticated_tab_count",
                        "statements_tab_count",
                        "login_recovery_performed",
                        "login_recovery_attempt_count",
                        "issue_summary",
                        "next_action",
                    )
                },
                indent=2,
                sort_keys=True,
            )
        )
    # A handoff is a successful control-plane response even when further
    # authentication work remains. This keeps local-model runners from
    # treating the deterministic next action as a failed command. The
    # structured handoff status remains the source of truth for whether any
    # downstream work may run.
    if args.handoff:
        return 0
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
