#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import signal
import socket
import sys
import time
import urllib.parse
import urllib.request

import websocket  # type: ignore[import-not-found]


LOGIN_URL = "https://app.baselane.com/login"
TRANSACTIONS_URL = "https://app.baselane.com/transactions"


class CdpDeadlineExceeded(TimeoutError):
    pass


def with_alarm(seconds: float):
    if not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"):
        return None
    previous_handler = signal.getsignal(signal.SIGALRM)

    def handler(_signum, _frame):
        raise CdpDeadlineExceeded(f"operation timed out after {seconds:g}s")

    signal.signal(signal.SIGALRM, handler)
    signal.setitimer(signal.ITIMER_REAL, max(0.25, float(seconds)))
    return previous_handler


def clear_alarm(previous_handler) -> None:
    if previous_handler is None:
        return
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, previous_handler)


def version_url() -> str:
    raw = (os.environ.get("BASELANE_CDP_VERSION_URL") or os.environ.get("BASELANE_CDP_URL") or "").strip()
    if raw:
        return raw if raw.endswith("/json/version") else raw.rstrip("/") + "/json/version"
    host = os.environ.get("BASELANE_CDP_HOST") or "127.0.0.1"
    port = os.environ.get("BASELANE_CDP_PORT") or "9222"
    return f"http://{host}:{port}/json/version"


def cdp_base_url() -> str:
    raw = version_url()
    return raw[: -len("/json/version")] if raw.endswith("/json/version") else raw.rstrip("/")


def cdp_request(url: str, timeout: float, method: str = "GET"):
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or '').lower()
    request = urllib.request.Request(url, method=method)
    if host not in {"127.0.0.1", "localhost", "::1"} and parsed.port in {9222, 19222, 19223}:
        request.add_header("Host", "localhost")
    return urllib.request.urlopen(request, timeout=timeout)


def rewrite_cdp_websocket_url(ws_url: str, endpoint_url: str) -> str:
    ws = urllib.parse.urlsplit(ws_url)
    endpoint = urllib.parse.urlsplit(endpoint_url)
    ws_host = (ws.hostname or '').lower()
    endpoint_host = endpoint.hostname or ''
    if ws_host not in {"127.0.0.1", "localhost", "::1"}:
        return ws_url
    if endpoint_host.lower() in {"127.0.0.1", "localhost", "::1", ""} and ws.port and endpoint.port is None:
        return ws_url
    port = endpoint.port or ws.port
    if port is None:
        return ws_url
    host = endpoint_host or ws.hostname or ""
    host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return urllib.parse.urlunsplit((ws.scheme, f"{host}:{port}", ws.path, ws.query, ws.fragment))


def request_json_url(path: str, timeout: float, method: str = "GET"):
    with cdp_request(cdp_base_url() + path, timeout=timeout, method=method) as response:
        body = response.read().decode("utf-8", errors="replace")
        return json.loads(body)


def create_target(cdp: "Cdp", url: str, timeout: float) -> dict:
    try:
        created = cdp.send("Target.createTarget", {"url": url}, timeout=timeout)
        if created.get("targetId"):
            return created
    except Exception as exc:
        print(f"WARN: Target.createTarget failed; trying /json/new fallback: {exc}", file=sys.stderr)
    encoded = urllib.parse.quote(url, safe="")
    last_error: Exception | None = None
    for method in ("PUT", "GET"):
        try:
            opened = request_json_url(f"/json/new?{encoded}", timeout=timeout, method=method)
            target_id = opened.get("id") or opened.get("targetId")
            if target_id:
                return {"targetId": target_id, "openedVia": f"json_new_{method.lower()}"}
        except Exception as exc:
            last_error = exc
    if last_error:
        raise last_error
    raise RuntimeError("/json/new did not return a target id")


class Cdp:
    def __init__(self, ws_url: str, timeout: float) -> None:
        self.ws = websocket.WebSocket()
        previous_handler = with_alarm(timeout + 1)
        try:
            self.ws.connect(ws_url, timeout=timeout, origin=None, suppress_origin=True)
            self.ws.settimeout(timeout)
        finally:
            clear_alarm(previous_handler)
        self.next_id = 1
        self.timeout = timeout

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass

    def send(self, method: str, params: dict | None = None, session_id: str | None = None, timeout: float | None = None) -> dict:
        effective_timeout = timeout or self.timeout
        previous_handler = with_alarm(effective_timeout + 1)
        msg: dict = {"id": self.next_id, "method": method}
        try:
            if params is not None:
                msg["params"] = params
            if session_id:
                msg["sessionId"] = session_id
            wanted = self.next_id
            self.next_id += 1
            self.ws.send(json.dumps(msg))
            deadline = time.time() + effective_timeout
            while time.time() < deadline:
                self.ws.settimeout(max(0.1, deadline - time.time()))
                try:
                    raw = self.ws.recv()
                except (TimeoutError, websocket.WebSocketTimeoutException):
                    if time.time() >= deadline:
                        break
                    continue
                reply = json.loads(raw)
                if reply.get("id") != wanted:
                    continue
                if reply.get("error"):
                    raise RuntimeError(json.dumps(reply["error"]))
                return reply.get("result") or {}
            raise TimeoutError(f"CDP {method} timed out")
        finally:
            clear_alarm(previous_handler)


def eval_expr(cdp: Cdp, session_id: str, expression: str, timeout: float | None = None):
    result = cdp.send(
        "Runtime.evaluate",
        {"expression": expression, "awaitPromise": True, "returnByValue": True},
        session_id,
        timeout,
    )
    return (result.get("result") or {}).get("value")


def attach(cdp: Cdp, target_id: str) -> str:
    result = cdp.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
    session_id = result["sessionId"]
    try:
        cdp.send("Page.enable", {}, session_id)
    except Exception:
        pass
    cdp.send("Runtime.enable", {}, session_id)
    try:
        cdp.send("Page.bringToFront", {}, session_id)
    except Exception:
        pass
    return session_id


def app_state(cdp: Cdp, session_id: str) -> dict:
    return eval_expr(
        cdp,
        session_id,
        r"""(() => {
          const href = String(location.href || '');
          const text = String(document.body && document.body.innerText || '').trim();
          const visible = s => Array.from(document.querySelectorAll(s)).some(el => {
            const style = window.getComputedStyle(el);
            const r = el.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none' && !el.disabled && r.width > 0 && r.height > 0;
          });
          const loginVisible = visible('input[name="email"],input#email,input[type="email"],input[autocomplete="email"]')
            && visible('input[name="password"],input#password,input[type="password"],input[autocomplete="current-password"]');
          const loadingOnly = !text || /^Loading\.*$/i.test(text);
          const contentReady = href.startsWith('https://app.baselane.com/')
            && !href.includes('/login')
            && !href.includes('/session-expired')
            && !href.includes('/access-denied')
            && !href.includes('/error')
            && !loginVisible
            && !loadingOnly
            && !/session expired|sign in/i.test(text)
            && /(Transactions|Dashboard|Properties|Banking)/i.test(text);
          return {href, loginVisible, loadingOnly, contentReady, bodyExcerpt: text.slice(0, 160)};
        })()""",
    ) or {}


def wait_for_app_content(cdp: Cdp, session_id: str, seconds: int) -> dict:
    state: dict = {}
    for _ in range(seconds):
        time.sleep(1)
        try:
            state = app_state(cdp, session_id)
        except Exception:
            pass
        if state.get("contentReady"):
            return state
        if any(marker in str(state.get("href") or "") for marker in ("/login", "/session-expired", "/access-denied", "/error")) or state.get("loginVisible"):
            break
    return state


def main() -> int:
    email = os.environ.get("BASELANE_EMAIL") or ""
    password = os.environ.get("BASELANE_PASSWORD") or ""
    timeout_seconds = max(30, int(float(os.environ.get("BASELANE_SESSION_SEED_TIMEOUT_SECONDS") or "180")))
    command_timeout = max(3.0, float(os.environ.get("BASELANE_CDP_COMMAND_TIMEOUT_MS") or "12"))
    socket.setdefaulttimeout(command_timeout)
    deadline = time.time() + timeout_seconds

    previous_handler = with_alarm(command_timeout + 1)
    try:
        version_endpoint = version_url()
        with cdp_request(version_endpoint, timeout=command_timeout) as response:
            version = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        print(f"ERROR: CDP /json/version unavailable: {type(exc).__name__}: {exc}")
        return 6
    finally:
        clear_alarm(previous_handler)
    try:
        cdp = Cdp(rewrite_cdp_websocket_url(version["webSocketDebuggerUrl"], version_endpoint), timeout=command_timeout)
    except Exception as exc:
        print(f"ERROR: CDP browser websocket unavailable: {type(exc).__name__}: {exc}")
        return 6
    try:
        session_id = None
        fresh_first = os.environ.get("BASELANE_SEED_FRESH_TARGET_FIRST") == "1"
        if fresh_first:
            try:
                created = create_target(cdp, TRANSACTIONS_URL, command_timeout)
                session_id = attach(cdp, created["targetId"])
            except Exception as exc:
                print(f"WARN: fresh Baselane target creation/attach failed: {exc}", file=sys.stderr)
                session_id = None
        if not session_id:
            targets = cdp.send("Target.getTargets")
            pages = [
                t for t in targets.get("targetInfos", [])
                if t.get("type") == "page" and "baselane.com" in str(t.get("url") or "")
            ]
            ordered = [
                *[t for t in pages if "/transactions" in str(t.get("url") or "")],
                *[t for t in pages if "/login" in str(t.get("url") or "")],
                *[t for t in pages if "/transactions" not in str(t.get("url") or "") and "/login" not in str(t.get("url") or "")],
            ][:3]
            for target in ordered:
                try:
                    session_id = attach(cdp, target["targetId"])
                    break
                except Exception as exc:
                    print(f"WARN: Baselane target attach failed ({target.get('targetId')}): {exc}", file=sys.stderr)
        if not session_id:
            try:
                created = create_target(cdp, TRANSACTIONS_URL, command_timeout)
                session_id = attach(cdp, created["targetId"])
            except Exception as exc:
                print(f"ERROR: CDP target unavailable: {type(exc).__name__}: {exc}")
                return 6

        # Check if the existing tab is already authenticated before navigating
        try:
            pre_state = app_state(cdp, session_id)
        except Exception:
            pre_state = {}
        if pre_state.get("contentReady"):
            print(f"SKIP: existing authenticated session ({pre_state.get('href')})")
            return 0
        # Tab not ready; navigate to transactions and wait for content
        try:
            cdp.send("Page.navigate", {"url": TRANSACTIONS_URL}, session_id)
        except Exception:
            pass
        state = wait_for_app_content(cdp, session_id, min(30, max(1, int(deadline - time.time()))))
        if state.get("contentReady"):
            print(f"SKIP: existing authenticated session ({state.get('href')})")
            return 0
        if state.get("loadingOnly"):
            print("INFO: Baselane URL loaded without authenticated content; reseeding session")
        if not email or not password:
            print("ERROR: missing BASELANE_EMAIL/BASELANE_PASSWORD")
            return 1

        try:
            cdp.send("Page.navigate", {"url": LOGIN_URL}, session_id)
        except Exception:
            pass
        found = False
        for _ in range(30):
            try:
                has_inputs = bool(eval_expr(cdp, session_id, "!!(document.querySelector('input[name=\"email\"],input#email')&&document.querySelector('input[name=\"password\"],input#password'))", 5))
            except Exception:
                has_inputs = False
            if has_inputs:
                found = True
                break
            time.sleep(1)
        if not found:
            print("ERROR: login inputs not present")
            return 2

        eval_expr(cdp, session_id, "document.querySelectorAll('#signInButtonAppleSSO,#signInButtonSSO,button[id*=\"Apple\"],button[id*=\"SSO\"],button[id*=\"Google\"]').forEach(b=>{b.disabled=true;b.style.pointerEvents='none';b.style.display='none';}); true")
        set_expr = f"""(() => {{ const set=(sel,val)=>{{const el=document.querySelector(sel);if(!el)return false;const s=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;s.call(el,val);el.dispatchEvent(new Event('input',{{bubbles:true}}));el.dispatchEvent(new Event('change',{{bubbles:true}}));el.dispatchEvent(new Event('blur',{{bubbles:true}}));return true;}}; return set('input[name="email"],input#email', {json.dumps(email)}) && set('input[name="password"],input#password', {json.dumps(password)}); }})()"""
        if not eval_expr(cdp, session_id, set_expr):
            print("ERROR: could not set inputs")
            return 3

        time.sleep(0.7)
        rect = eval_expr(cdp, session_id, "(() => { const b=document.querySelector('#signInButton'); if(!b)return null; const r=b.getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2,vis:!!(b.offsetWidth||b.offsetHeight),disabled:b.disabled}; })()")
        if not rect or not rect.get("vis") or rect.get("disabled"):
            print("ERROR: #signInButton not clickable")
            return 4
        cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": rect["x"], "y": rect["y"]}, session_id)
        cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": rect["x"], "y": rect["y"], "button": "left", "clickCount": 1}, session_id)
        time.sleep(0.06)
        cdp.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": rect["x"], "y": rect["y"], "button": "left", "clickCount": 1}, session_id)

        state = wait_for_app_content(cdp, session_id, min(60, max(1, int(deadline - time.time()))))
        if state.get("contentReady"):
            print(f"SEEDED: session established ({state.get('href')})")
            return 0
        print(f"ERROR: authenticated Baselane content not confirmed ({state.get('href') or 'unknown'}; {str(state.get('bodyExcerpt') or '')[:80]})")
        return 5
    finally:
        cdp.close()


if __name__ == "__main__":
    raise SystemExit(main())
