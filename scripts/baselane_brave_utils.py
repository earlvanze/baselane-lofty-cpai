#!/usr/bin/env python3
import argparse
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

DEDICATED_BRAVE_CDP_PORT = 19222
DEFAULT_BRAVE_CDP_PORT = DEDICATED_BRAVE_CDP_PORT
ISSUE_CLASS = 'baselane-brave-utils'
SCRIPT_PATH = Path(__file__).resolve()


def diagnostic_command() -> str:
    return f'python3 {shlex.quote(str(SCRIPT_PATH))} --json'


DIAGNOSTIC_COMMAND = diagnostic_command()


def brave_port_candidates(env=None):
    if env is None:
        env = os.environ
    raw = str(env.get('BASELANE_CDP_PORT', str(DEFAULT_BRAVE_CDP_PORT))).strip()
    issues = []
    try:
        port = int(raw)
        if port != DEDICATED_BRAVE_CDP_PORT:
            raise ValueError(f'only dedicated Baselane CDP port {DEDICATED_BRAVE_CDP_PORT} is allowed')
        return [port], issues
    except Exception as exc:
        issues.append(f'BASELANE_CDP_PORT is invalid ({raw!r}): {exc}')
        return [DEFAULT_BRAVE_CDP_PORT], issues


BRAVE_PORT_CANDIDATES = brave_port_candidates()[0]
STATIC_HOST_CANDIDATES = ['127.0.0.1', 'localhost', '172.21.128.1']


def load_env(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        if '=' in line and not line.strip().startswith('#'):
            k, v = line.split('=', 1)
            os.environ[k.strip()] = v.strip()


def http_json(url: str, timeout: int = 5):
    # Brave CDP on Windows rejects non-localhost Host headers
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or '').lower()
    if host in {'127.0.0.1', 'localhost', '::1'} or parsed.port not in {9222, 19222, 19223}:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8'))
    req = urllib.request.Request(url)
    if parsed.port in {9222, 19222, 19223}:
        req.add_header('Host', 'localhost')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def rewrite_cdp_websocket_url(ws_url: str, endpoint_url: str) -> str:
    ws = urllib.parse.urlsplit(ws_url)
    endpoint = urllib.parse.urlsplit(endpoint_url)
    ws_host = (ws.hostname or '').lower()
    endpoint_host = endpoint.hostname or ''
    if ws_host not in {'127.0.0.1', 'localhost', '::1'}:
        return ws_url
    if endpoint_host.lower() in {'127.0.0.1', 'localhost', '::1', ''} and ws.port and endpoint.port is None:
        return ws_url
    port = endpoint.port or ws.port
    if port is None:
        return ws_url
    host = endpoint_host or ws.hostname or ''
    host = f'[{host}]' if ':' in host and not host.startswith('[') else host
    return urllib.parse.urlunsplit((ws.scheme, f'{host}:{port}', ws.path, ws.query, ws.fragment))


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
        custom_port = urllib.parse.urlsplit(custom).port
        if custom_port == DEDICATED_BRAVE_CDP_PORT:
            candidates.append(custom)
        else:
            last_error = (
                f'{custom}: only dedicated Baselane CDP port '
                f'{DEDICATED_BRAVE_CDP_PORT} is allowed'
            )
    for host in brave_host_candidates():
        for port in brave_port_candidates()[0]:
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
        if urllib.parse.urlsplit(explicit_ws).port != DEDICATED_BRAVE_CDP_PORT:
            raise RuntimeError(
                f'BASELANE_CDP_URL must use dedicated Baselane CDP port {DEDICATED_BRAVE_CDP_PORT}'
            )
        return explicit_ws
    info = resolve_brave_cdp_version(timeout=timeout)
    return rewrite_cdp_websocket_url(info['ws_url'], info.get('version_url', ''))


def cdp_diagnostics():
    out = {'checked_at': time.time(), 'host_candidates': brave_host_candidates(), 'checks': []}
    explicit_ws = os.environ.get('BASELANE_CDP_URL', '').strip()
    if explicit_ws:
        out['checks'].append({'kind': 'explicit_ws', 'value': explicit_ws})
    custom = os.environ.get('BASELANE_CDP_VERSION_URL', '').strip()
    candidates = []
    if custom and urllib.parse.urlsplit(custom).port == DEDICATED_BRAVE_CDP_PORT:
        candidates.append(custom)
    elif custom:
        out['rejected_configured_endpoint'] = custom
    for host in brave_host_candidates():
        for port in brave_port_candidates()[0]:
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


def remediation_fields(classification: str):
    has_issues = classification != 'ok'
    return {
        'remediation_class': 'operator-reviewed-baselane-brave-utils' if has_issues else 'no-remediation-needed',
        'requires_operator_approval': has_issues,
        'requires_interactive_sudo': False,
        'requires_interactive_oauth': False,
        'safe_to_run_automatically': not has_issues,
        'review_command': diagnostic_command(),
        'review_command_safe_to_run_automatically': True,
        'cleanup_command_after_review': None,
        'restart_command_after_review': None,
        'oauth_command_after_review': None,
        'helper_command_after_review': None,
    }


def review_command_validation(command: object | None = None):
    command = diagnostic_command() if command is None else command
    expected_script = str(SCRIPT_PATH)
    issues = []
    parts = []
    if not isinstance(command, str) or not command.strip():
        issues.append('review command is empty or not a string')
    else:
        try:
            parts = shlex.split(command)
        except ValueError as exc:
            issues.append(f'review command is not shell-parseable: {exc}')
    script_exists = SCRIPT_PATH.exists()
    script_is_file = SCRIPT_PATH.is_file()
    python3_present = bool(parts) and parts[0] == 'python3'
    script_path_present = expected_script in parts
    json_flag_present = '--json' in parts
    if not python3_present:
        issues.append('review command must start with python3')
    if not script_path_present:
        issues.append(f'review command must include script path: {expected_script}')
    if not json_flag_present:
        issues.append('review command must include --json')
    if not script_exists:
        issues.append(f'review command script path does not exist: {expected_script}')
    elif not script_is_file:
        issues.append(f'review command script path is not a file: {expected_script}')
    return {
        'command': command,
        'expected_script_path': expected_script,
        'script_exists': script_exists,
        'script_is_file': script_is_file,
        'path': expected_script,
        'path_exists': script_exists,
        'python3_present': python3_present,
        'script_path_present': script_path_present,
        'json_flag_present': json_flag_present,
        'requires_executable': False,
        'valid': not issues,
        'issues': issues,
        'issue': issues[0] if issues else None,
    }


def _diagnostic_host_candidates(env=None):
    if env is None:
        env = os.environ
    hosts = []
    explicit = str(env.get('BASELANE_CDP_HOST', '')).strip()
    if explicit:
        hosts.append(explicit)
    for cand in STATIC_HOST_CANDIDATES:
        if cand not in hosts:
            hosts.append(cand)
    return hosts


def _candidate_version_urls(env=None):
    if env is None:
        env = os.environ
    ports, _ = brave_port_candidates(env)
    candidates = []
    custom = str(env.get('BASELANE_CDP_VERSION_URL', '')).strip()
    if custom:
        candidates.append(custom)
    for host in _diagnostic_host_candidates(env):
        for port in ports:
            candidates.append(f'http://{host}:{port}/json/version')
    seen = set()
    deduped = []
    for url in candidates:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped


def classified_issue_records(issues, evidence, classification):
    fields = remediation_fields(classification)
    validation = review_command_validation(fields.get('review_command'))
    return [
        {
            'issue': issue,
            'issue_class': ISSUE_CLASS,
            'classification': classification,
            'area': 'baselane-cdp-utility',
            'env_cdp_host_present': evidence.get('env_cdp_host_present'),
            'env_cdp_url_present': evidence.get('env_cdp_url_present'),
            'env_version_url_present': evidence.get('env_version_url_present'),
            'cdp_port_candidates': evidence.get('cdp_port_candidates'),
            'ip_command_available': evidence.get('ip_command_available'),
            'http_probe_attempted': evidence.get('http_probe_attempted'),
            'ip_route_subprocess_attempted': evidence.get('ip_route_subprocess_attempted'),
            'review_command_valid': validation['valid'],
            'review_command_validation': validation,
            **fields,
        }
        for issue in issues
    ]


def classified_issue_summary(report):
    classified = report.get('classified_issues') or []
    class_counts = {}
    route_counts = {}
    validation_issues = []
    for issue in classified:
        issue_class = issue.get('issue_class')
        route = issue.get('classification', report.get('classification'))
        if issue_class:
            class_counts[issue_class] = class_counts.get(issue_class, 0) + 1
        if route:
            route_counts[route] = route_counts.get(route, 0) + 1
        validation_issues.extend(issue.get('review_command_validation', {}).get('issues') or [])
    safe_review_command_count = sum(1 for issue in classified if issue.get('review_command_safe_to_run_automatically'))
    valid_review_command_count = sum(1 for issue in classified if issue.get('review_command_valid') is True)
    return {
        'total': len(classified),
        'total_count': len(classified),
        'ok_count': int(report.get('ok_count') or 0),
        'issue_count': int(report.get('issue_count') or 0),
        'visible_ok_count': len(report.get('visible_ok') or []),
        'class_counts': class_counts,
        'issue_class_counts': class_counts,
        'route_classification': report.get('classification'),
        'route_classification_counts': route_counts,
        'approval_required_count': sum(1 for issue in classified if issue.get('requires_operator_approval')),
        'review_required_count': int(report.get('review_required_count') or 0),
        'interactive_sudo_count': sum(1 for issue in classified if issue.get('requires_interactive_sudo')),
        'interactive_oauth_count': sum(1 for issue in classified if issue.get('requires_interactive_oauth')),
        'safe_review_command_count': safe_review_command_count,
        'valid_review_command_count': valid_review_command_count,
        'invalid_review_command_count': safe_review_command_count - valid_review_command_count,
        'review_command_validation_issues': sorted(set(validation_issues)),
        'safe_to_run_automatically': report.get('safe_to_run_automatically') is True,
        'env_cdp_host_present': report.get('env_cdp_host_present') is True,
        'env_cdp_url_present': report.get('env_cdp_url_present') is True,
        'env_version_url_present': report.get('env_version_url_present') is True,
        'cdp_port_candidate_count': len(report.get('cdp_port_candidates') or []),
        'candidate_url_count': len(report.get('local_candidate_urls') or []),
        'ip_command_available': report.get('ip_command_available') is True,
        'http_probe_attempted': report.get('http_probe_attempted') is True,
        'ip_route_subprocess_attempted': report.get('ip_route_subprocess_attempted') is True,
        'browser_launch_attempted': report.get('browser_launch_attempted') is True,
        'write_attempted': report.get('write_attempted') is True,
        'remediation_class': report.get('remediation_class'),
        'cleanup_command_available_after_review': bool(report.get('cleanup_command_after_review')),
        'restart_command_available_after_review': bool(report.get('restart_command_after_review')),
        'oauth_command_available_after_review': bool(report.get('oauth_command_after_review')),
        'helper_command_available_after_review': bool(report.get('helper_command_after_review')),
    }


def build_report(env=None):
    if env is None:
        env = os.environ
    ports, port_issues = brave_port_candidates(env)
    issues = list(port_issues)
    visible_ok = []
    evidence = {
        'env_cdp_host_present': bool(str(env.get('BASELANE_CDP_HOST', '')).strip()),
        'env_cdp_url_present': bool(str(env.get('BASELANE_CDP_URL', '')).strip()),
        'env_version_url_present': bool(str(env.get('BASELANE_CDP_VERSION_URL', '')).strip()),
        'explicit_ws_url_present': bool(str(env.get('BASELANE_CDP_URL', '')).strip()),
        'custom_version_url': str(env.get('BASELANE_CDP_VERSION_URL', '')).strip() or None,
        'default_port': DEFAULT_BRAVE_CDP_PORT,
        'cdp_port_candidates': ports,
        'diagnostic_host_candidates': _diagnostic_host_candidates(env),
        'local_candidate_urls': _candidate_version_urls(env),
        'ip_command_available': shutil.which('ip') is not None,
        'http_probe_attempted': False,
        'ip_route_subprocess_attempted': False,
        'browser_launch_attempted': False,
        'write_attempted': False,
    }

    if not issues:
        visible_ok.append(
            'OK Baselane Brave utility config: '
            f'ports={ports} candidate_urls={len(evidence["local_candidate_urls"])}'
        )
        visible_ok.append(
            'OK Baselane Brave utility diagnostic: '
            'no CDP HTTP probe, ip route subprocess, browser launch, write, restart, sudo, OAuth, or helper command'
        )

    classification = 'baselane-brave-utils-review' if issues else 'ok'
    classified_issues = classified_issue_records(issues, evidence, classification)
    fields = remediation_fields(classification)
    report = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'status': 'BASELANE_BRAVE_UTILS_REVIEW' if issues else 'NO_REPLY',
        'classification': classification,
        'ok': visible_ok,
        'ok_state': not issues,
        'visible_ok': visible_ok,
        'ok_count': len(visible_ok),
        'issues': issues,
        'issue_count': len(issues),
        'issue_classes': [ISSUE_CLASS] if issues else [],
        'classified_issues': classified_issues,
        'advisory_count': 0,
        'review_required_count': len(classified_issues),
        **evidence,
        'remediation': {'classification': fields['remediation_class'], **fields},
        **fields,
    }
    summary = classified_issue_summary(report)
    report['classified_issue_summary'] = summary
    report['safe_review_command_count'] = summary['safe_review_command_count']
    report['valid_review_command_count'] = summary['valid_review_command_count']
    report['invalid_review_command_count'] = summary['invalid_review_command_count']
    report['review_command_validation_issues'] = summary['review_command_validation_issues']
    return report


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='Baselane Brave/CDP utility diagnostics')
    parser.add_argument('--json', action='store_true', help='Emit a read-only diagnostic report without probing CDP')
    return parser.parse_args(argv)


def main(argv=None, stdout: TextIO | None = None) -> int:
    args = parse_args(argv)
    if args.json:
        report = build_report()
        print(json.dumps(report, indent=2, sort_keys=True), file=stdout or sys.stdout)
        return 0 if report['status'] == 'NO_REPLY' else 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
