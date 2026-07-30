#!/usr/bin/env python3
import json
import os
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

BRAVE_PORT_CANDIDATES = [int(os.environ.get('BASELANE_CDP_PORT', '9222'))]
STATIC_HOST_CANDIDATES = ['127.0.0.1', 'localhost', '172.21.128.1']


def load_env(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        if '=' in line and not line.strip().startswith('#'):
            k, v = line.split('=', 1)
            os.environ[k.strip()] = v.strip()


def http_json(url: str, timeout: int = 5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def _linux_default_gateway():
    try:
        out = subprocess.check_output(['ip', 'route'], text=True)
        for line in out.splitlines():
            parts = line.split()
            if parts[:1] == ['default'] and 'via' in parts:
                return parts[parts.index('via') + 1]
    except Exception:
        pass
    return None


def _resolv_nameserver():
    try:
        for line in Path('/etc/resolv.conf').read_text(encoding='utf-8').splitlines():
            if line.startswith('nameserver '):
                ip = line.split(None, 1)[1].strip()
                return ip or None
    except Exception:
        pass
    return None


def _resolved_host(name: str):
    try:
        return socket.gethostbyname(name)
    except Exception:
        return None


def brave_host_candidates():
    hosts = []
    explicit = os.environ.get('BASELANE_CDP_HOST', '').strip()
    if explicit:
        hosts.append(explicit)
    for cand in [_linux_default_gateway(), _resolv_nameserver(), _resolved_host('host.docker.internal')]:
        if cand and cand not in hosts:
            hosts.append(cand)
    for cand in STATIC_HOST_CANDIDATES:
        if cand not in hosts:
            hosts.append(cand)
    return hosts


def resolve_brave_cdp_version(timeout: int = 5):
    last_error = None
    custom = os.environ.get('BASELANE_CDP_VERSION_URL', '').strip()
    candidates = []
    if custom:
        candidates.append(custom)
    for host in brave_host_candidates():
        for port in BRAVE_PORT_CANDIDATES:
            candidates.append(f'http://{host}:{port}/json/version')

    seen = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        try:
            data = http_json(url, timeout=timeout)
            ws = data.get('webSocketDebuggerUrl', '')
            browser = data.get('Browser', '')
            if ws and 'brave' in browser.lower():
                return {'version_url': url, 'ws_url': ws, 'browser': browser, 'ok': True}
            if ws:
                return {'version_url': url, 'ws_url': ws, 'browser': browser, 'ok': True, 'warning': 'browser_name_not_brave'}
            last_error = f'{url}: missing webSocketDebuggerUrl'
        except Exception as e:
            last_error = f'{url}: {e}'
    raise RuntimeError('Brave CDP not reachable. Tried: ' + ', '.join(candidates) + f'. Last error: {last_error}')


def brave_cdp_ws_url(timeout: int = 5) -> str:
    explicit_ws = os.environ.get('BASELANE_CDP_URL', '').strip()
    if explicit_ws:
        return explicit_ws
    return resolve_brave_cdp_version(timeout=timeout)['ws_url']


def cdp_diagnostics():
    out = {'checked_at': time.time(), 'host_candidates': brave_host_candidates(), 'checks': []}
    explicit_ws = os.environ.get('BASELANE_CDP_URL', '').strip()
    if explicit_ws:
        out['checks'].append({'kind': 'explicit_ws', 'value': explicit_ws})
    custom = os.environ.get('BASELANE_CDP_VERSION_URL', '').strip()
    candidates = []
    if custom:
        candidates.append(custom)
    for host in brave_host_candidates():
        for port in BRAVE_PORT_CANDIDATES:
            candidates.append(f'http://{host}:{port}/json/version')
    seen = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        item = {'url': url, 'ok': False}
        try:
            data = http_json(url, timeout=3)
            item['ok'] = True
            item['browser'] = data.get('Browser', '')
            item['ws'] = data.get('webSocketDebuggerUrl', '')
        except Exception as e:
            item['error'] = str(e)
        out['checks'].append(item)
    return out


def is_login_page(page) -> bool:
    url = (page.url or '').lower()
    if '/login' in url or '/signin' in url:
        return True
    try:
        return page.locator('input[name="email"], input#email, input[type="email"]').count() > 0
    except Exception:
        return False


def force_email_password_login(page, email: str, password: str, timeout_ms: int = 45000):
    email_sel = 'input[name="email"], input#email, input[type="email"], input[autocomplete="email"]'
    pass_sel = 'input[name="password"], input#password, input[type="password"], input[autocomplete="current-password"]'
    social_sel = '#signInButtonAppleSSO, #signInButtonSSO, button[id*="Apple"], button[id*="SSO"], button[id*="Google"]'

    page.goto('https://app.baselane.com/login', wait_until='domcontentloaded', timeout=60000)
    page.wait_for_selector(email_sel, timeout=timeout_ms)
    page.wait_for_selector(pass_sel, timeout=timeout_ms)

    page.evaluate(
        """
        (sel) => {
          for (const el of document.querySelectorAll(sel)) {
            el.setAttribute('disabled', 'disabled');
            el.style.pointerEvents = 'none';
            el.style.display = 'none';
          }
        }
        """,
        social_sel,
    )

    page.locator(email_sel).first.fill(email)
    page.locator(pass_sel).first.fill(password)

    submitted = page.evaluate(
        """
        () => {
          const email = document.querySelector('input[name="email"], input#email, input[type="email"]');
          const password = document.querySelector('input[name="password"], input#password, input[type="password"]');
          if (!email || !password) return 'missing_inputs';
          const form = email.form || password.form || email.closest('form') || password.closest('form');
          const submitBtn = document.querySelector('#signInButton');
          if (form) {
            if (typeof form.requestSubmit === 'function') {
              if (submitBtn && submitBtn.form === form) form.requestSubmit(submitBtn);
              else form.requestSubmit();
            } else {
              form.submit();
            }
            return 'form_submitted';
          }
          if (submitBtn) {
            submitBtn.click();
            return 'clicked_signin_button';
          }
          password.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', bubbles:true}));
          password.dispatchEvent(new KeyboardEvent('keyup', {key:'Enter', bubbles:true}));
          return 'enter_fallback';
        }
        """
    )

    try:
        page.wait_for_load_state('networkidle', timeout=15000)
    except Exception:
        pass

    return submitted


def detect_login_blockers(page):
    body = ''
    try:
        body = (page.inner_text('body') or '').lower()
    except Exception:
        pass
    url = (page.url or '').lower()
    return {
        'url': page.url,
        'still_on_login': ('/login' in url or '/signin' in url),
        'challenge_gate': any(k in body for k in ['verify you are human', 'cloudflare', 'captcha', 'challenge']),
        'credential_rejected': any(k in body for k in ['incorrect password', 'invalid email', 'invalid credentials', 'wrong password']),
        'has_email_input': ('email' in body),
    }
