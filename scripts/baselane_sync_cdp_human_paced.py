#!/usr/bin/env python3
"""
baselane_sync_cdp_human_paced.py

Compatibility entrypoint. By default this runs the deterministic monolithic
CDP sync path first because that is the reliable authenticated-session exporter.
If the primary path fails, it falls back to the older split login-wait +
human-paced exporter path unless BASELANE_ENABLE_HUMAN_PACED_BACKUP=0.
Set BASELANE_ENABLE_LEGACY_HUMAN_PACED=1 to run only the older split path for
explicit troubleshooting.
"""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import urllib.request
import urllib.parse
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

import baselane_sync_cdp_deterministic as deterministic_sync

ROOT = Path(os.environ.get('WORKSPACE_ROOT', str(Path.home() / '.openclaw' / 'workspace')))
OPENCLAW_ENV = ROOT.parent / '.env'
REPORT = ROOT / 'reports' / 'baselane_sync_cdp_report.json'
AUTH_PREFLIGHT = ROOT / 'scripts' / 'baselane_cdp_auth_recovery.py'
EXPORT_SCRIPT = ROOT / 'scripts' / 'baselane_export_human_paced.js'
DEFAULT_SYNC_SCRIPT = ROOT / 'scripts' / 'baselane_sync_cdp_deterministic.py'
SPLIT_SCRIPT = ROOT / 'scripts' / 'split_ledger_public_financials.py'
ISSUE_CLASS = 'baselane-sync-cdp-human-paced'
SCRIPT_PATH = Path(__file__).resolve()


def diagnostic_command() -> str:
    return f'python3 {shlex.quote(str(SCRIPT_PATH))} --json'


DIAGNOSTIC_COMMAND = diagnostic_command()
CDP_TARGETS = [
    ('127.0.0.1', 18800),
    ('127.0.0.1', 19222),
    ('127.0.0.1', 9222),
    ('::1', 18800),
    ('::1', 9222),
]


def cdp_urlopen(url, timeout=2):
    parsed = urllib.parse.urlsplit(url)
    request = url
    if parsed.hostname and parsed.hostname.lower().strip("[]") not in {"127.0.0.1", "localhost", "::1"}:
        request = urllib.request.Request(url, headers={"Host": "localhost"})
    return urllib.request.urlopen(request, timeout=timeout)


def load_env(path: Path, *, override: bool = False):
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        if '=' in line and not line.strip().startswith('#'):
            k, v = line.split('=', 1)
            key = k.strip()
            if override:
                os.environ[key] = v.strip()
            else:
                os.environ.setdefault(key, v.strip())


def cdp_version_url():
    configured = os.environ.get('BASELANE_CDP_VERSION_URL')
    candidates = []
    if configured:
        candidates.append(configured)
    for host, port in CDP_TARGETS:
        brackets = f'[{host}]' if ':' in host else host
        candidates.append(f'http://{brackets}:{port}/json/version')

    responsive = []
    for order, url in enumerate(dict.fromkeys(candidates)):
        try:
            with cdp_urlopen(url, timeout=2) as r:
                r.read(1)
        except Exception:
            continue

        score = 0
        try:
            parsed = urllib.parse.urlsplit(url)
            pages_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, '/json', '', ''))
            with cdp_urlopen(pages_url, timeout=2) as response:
                pages = json.load(response)
            page_urls = [str(page.get('url') or '').lower() for page in pages if page.get('type') == 'page']
            if any('app.baselane.com/' in page_url and not any(
                marker in page_url for marker in ('/login', '/session-expired')
            ) for page_url in page_urls):
                score = 2
            elif any('app.baselane.com/' in page_url for page_url in page_urls):
                score = 1
        except Exception:
            pass
        responsive.append((score, -order, url))

    if not responsive:
        return None
    return max(responsive)[2]


def ensure_cdp_running():
    return cdp_version_url() is not None


def write_report(report):
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2), encoding='utf-8')


def _env_enabled(name: str, default: str = '0') -> bool:
    return str(os.environ.get(name, default)).strip().lower() not in {'', '0', 'false', 'no', 'off'}


def run_primary_deterministic(started_at):
    env = os.environ.copy()
    env.setdefault('BASELANE_SKIP_LOGIN_WAIT', '1')
    env.setdefault('BASELANE_FORCE_LOGIN', '0')
    result = subprocess.run(['python3', str(DEFAULT_SYNC_SCRIPT)], text=True, capture_output=True, env=env, timeout=2400)
    return {
        'started_at': started_at,
        'status': 'ok' if result.returncode == 0 else 'failed',
        'mode': 'deterministic_primary_human_paced_backup',
        'compat_entrypoint': str(SCRIPT_PATH),
        'primary_script': str(DEFAULT_SYNC_SCRIPT),
        'primary_return_code': result.returncode,
        'primary_stdout_tail': (result.stdout or '')[-4000:],
        'primary_stderr_tail': (result.stderr or '')[-4000:],
        'backup_enabled': _env_enabled('BASELANE_ENABLE_HUMAN_PACED_BACKUP', '1'),
        'backup_attempted': False,
        'backup_status': 'not_needed' if result.returncode == 0 else 'not_attempted',
    }, result.returncode


def primary_export_succeeded(report):
    return (
        report.get('primary_return_code') == 5
        and 'split script failed' in str(report.get('primary_stdout_tail') or '').lower()
    )


def run_legacy_human_paced(report=None):
    if report is None:
        report = {
            'started_at': time.time(),
            'status': 'running',
            'steps': [],
            'mode': 'human_paced_legacy_only',
            'backup_enabled': False,
            'backup_attempted': False,
            'backup_status': 'not_applicable',
        }
    else:
        report.setdefault('steps', [])
        report['backup_attempted'] = True
        report['backup_status'] = 'running'

    selected_cdp_version_url = cdp_version_url()
    if not selected_cdp_version_url:
        report['status'] = 'failed'
        report['reason'] = 'cdp_not_running'
        report['backup_status'] = 'failed'
        report['finished_at'] = time.time()
        write_report(report)
        print('FAILED: CDP not running (no probed CDP target responded)')
        return 4
    report['cdp_version_url'] = selected_cdp_version_url

    # Step 1: Verify an existing human-authenticated visible browser session.
    login = subprocess.run(
        [sys.executable, str(AUTH_PREFLIGHT), '--cdp-url', selected_cdp_version_url,
         '--report', str(ROOT / 'reports' / 'baselane_cdp_auth_recovery_report.json')],
        text=True, capture_output=True, timeout=60,
    )
    report['steps'].append('human_session_preflight')
    report['login_exit'] = login.returncode
    report['login_stdout_tail'] = (login.stdout or '')[-4000:]
    report['login_stderr_tail'] = (login.stderr or '')[-4000:]
    if login.returncode != 0:
        report['status'] = 'failed'
        report['reason'] = 'cdp_login_failed'
        report['backup_status'] = 'failed'
        report['finished_at'] = time.time()
        write_report(report)
        print('FAILED: a human-authenticated Baselane browser session is required')
        return 4

    # Human-like pause after login
    print('[SYNC] Existing human-authenticated session verified, waiting 3s before export...')
    time.sleep(3)

    # Step 2: Human-paced export
    export_env = os.environ.copy()
    export_env.setdefault('BASELANE_CDP_VERSION_URL', selected_cdp_version_url)
    export_env.setdefault('WORKSPACE_ROOT', str(ROOT))
    export_env.setdefault('OPENCLAW_ROOT', str(ROOT.parent))
    export_env.setdefault('BASELANE_LEDGER_DIR', '/mnt/c/Users/digit/Dropbox/Projects/assetrail')
    export_env.setdefault(
        'BASELANE_LEDGER_PATH',
        str(Path(export_env['BASELANE_LEDGER_DIR']) / 'ECO Systems General Ledger.csv'),
    )
    export = subprocess.run(
        ['node', str(EXPORT_SCRIPT)],
        text=True, capture_output=True, env=export_env, timeout=1800
    )
    report['steps'].append('human_paced_export')
    report['export_exit'] = export.returncode
    report['export_stdout_tail'] = (export.stdout or '')[-4000:]
    report['export_stderr_tail'] = (export.stderr or '')[-4000:]

    if export.returncode != 0:
        report['status'] = 'failed'
        report['reason'] = 'human_paced_export_failed'
        report['backup_status'] = 'failed'
        report['finished_at'] = time.time()
        write_report(report)
        print('FAILED: human-paced export failed')
        return 4

    canonical_ledger = Path(export_env['BASELANE_LEDGER_DIR']) / 'ECO Systems General Ledger.csv'
    report['dao_eco_rehydration'] = deterministic_sync.rehydrate_indexed_dao_eco_rows(
        canonical_ledger,
        ROOT / 'reports' / 'baselane_source_transaction_index.csv',
    )
    if report['dao_eco_rehydration'].get('status') != 'ok':
        report['status'] = 'failed'
        report['reason'] = 'dao_eco_rehydration_failed'
        report['backup_status'] = 'failed'
        report['finished_at'] = time.time()
        write_report(report)
        return 4

    # Any June local-ledger replay is explicitly enabled for a reviewed recovery.
    normalization_rc = deterministic_sync.run_local_retained_capital_normalization(ROOT, report)
    report['local_retained_capital_normalization_rc'] = normalization_rc
    if normalization_rc != 0:
        report['status'] = 'review'
        report['reason'] = 'local_retained_capital_normalization_review'
        report['backup_status'] = 'review'
        report['finished_at'] = time.time()
        write_report(report)
        print('REVIEW: scoped local retained-capital normalization failed before split')
        return 2

    # Step 3: Split ledger
    rs = subprocess.run(
        ['python3', str(SPLIT_SCRIPT)],
        text=True, capture_output=True, timeout=1800
    )
    report['steps'].append('split_ledger')
    report['split_exit'] = rs.returncode
    report['split_stdout_tail'] = (rs.stdout or '')[-2000:]
    report['split_stderr_tail'] = (rs.stderr or '')[-2000:]

    report['status'] = 'ok' if rs.returncode == 0 else 'failed'
    report['backup_status'] = 'ok' if rs.returncode == 0 else 'failed'
    report['finished_at'] = time.time()
    write_report(report)

    if rs.returncode == 0:
        print('OK: baselane sync via human-paced export complete')
        return 0
    print('FAILED: split script failed')
    return 5


def run():
    if _env_enabled('BASELANE_ENABLE_LEGACY_HUMAN_PACED', '0'):
        return run_legacy_human_paced()

    started_at = time.time()
    report, primary_code = run_primary_deterministic(started_at)
    if primary_code == 0:
        report['finished_at'] = time.time()
        write_report(report)
        print('OK: baselane sync via deterministic monolithic exporter complete')
        return 0


    if primary_export_succeeded(report):
        report['finished_at'] = time.time()
        report['status'] = 'failed'
        report['reason'] = 'primary_export_ok_split_failed'
        report['backup_status'] = 'not_attempted_export_already_current'
        write_report(report)
        print('FAILED: deterministic export succeeded but split script failed')
        return primary_code

    if not report['backup_enabled']:
        report['finished_at'] = time.time()
        write_report(report)
        print('FAILED: deterministic monolithic exporter failed and human-paced backup is disabled')
        return primary_code

    print('WARN: deterministic monolithic exporter failed; attempting human-paced backup')
    report['reason'] = 'human_paced_backup_after_primary_failure'
    return run_legacy_human_paced(report)


def _paths_for_root(root: Path):
    return {
        'root': root,
        'report': root / 'reports' / 'baselane_sync_cdp_report.json',
        'auth_preflight': root / 'scripts' / 'baselane_cdp_auth_recovery.py',
        'export_script': root / 'scripts' / 'baselane_export_human_paced.js',
        'default_sync_script': root / 'scripts' / 'baselane_sync_cdp_deterministic.py',
        'split_script': root / 'scripts' / 'split_ledger_public_financials.py',
    }


def remediation_fields(classification: str):
    has_issues = classification != 'ok'
    return {
        'remediation_class': 'operator-reviewed-baselane-sync-cdp-human-paced' if has_issues else 'no-remediation-needed',
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


def _path_state(path: Path):
    return {
        'path': str(path),
        'exists': path.exists(),
        'readable': path.is_file() and os.access(path, os.R_OK),
        'size_bytes': path.stat().st_size if path.exists() and path.is_file() else None,
    }


def _target_urls():
    urls = []
    for host, port in CDP_TARGETS:
        brackets = f'[{host}]' if ':' in host else host
        urls.append(f'http://{brackets}:{port}/json/version')
    return urls


def classified_issue_records(issues, evidence, classification):
    fields = remediation_fields(classification)
    validation = review_command_validation(fields.get('review_command'))
    return [
        {
            'issue': issue,
            'issue_class': ISSUE_CLASS,
            'classification': classification,
            'area': 'baselane-human-paced-cdp-sync',
            'node_available': evidence.get('node_available'),
            'python3_available': evidence.get('python3_available'),
            'login_script_readable': evidence.get('login_script', {}).get('readable'),
            'export_script_readable': evidence.get('export_script', {}).get('readable'),
            'split_script_readable': evidence.get('split_script', {}).get('readable'),
            'report_parent_exists': evidence.get('report_parent_exists'),
            'cdp_probe_attempted': evidence.get('cdp_probe_attempted'),
            'login_subprocess_attempted': evidence.get('login_subprocess_attempted'),
            'export_subprocess_attempted': evidence.get('export_subprocess_attempted'),
            'split_subprocess_attempted': evidence.get('split_subprocess_attempted'),
            'report_write_attempted': evidence.get('report_write_attempted'),
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
        'node_available': report.get('node_available') is True,
        'python3_available': report.get('python3_available') is True,
        'login_script_readable': report.get('login_script', {}).get('readable') is True,
        'export_script_readable': report.get('export_script', {}).get('readable') is True,
        'split_script_readable': report.get('split_script', {}).get('readable') is True,
        'report_parent_exists': report.get('report_parent_exists') is True,
        'report_parent_writable': report.get('report_parent_writable') is True,
        'secrets_file_exists': report.get('secrets_file_exists') is True,
        'secrets_read_attempted': report.get('secrets_read_attempted') is True,
        'cdp_probe_attempted': report.get('cdp_probe_attempted') is True,
        'login_subprocess_attempted': report.get('login_subprocess_attempted') is True,
        'human_pause_attempted': report.get('human_pause_attempted') is True,
        'export_subprocess_attempted': report.get('export_subprocess_attempted') is True,
        'split_subprocess_attempted': report.get('split_subprocess_attempted') is True,
        'report_write_attempted': report.get('report_write_attempted') is True,
        'remediation_class': report.get('remediation_class'),
        'cleanup_command_available_after_review': bool(report.get('cleanup_command_after_review')),
        'restart_command_available_after_review': bool(report.get('restart_command_after_review')),
        'oauth_command_available_after_review': bool(report.get('oauth_command_after_review')),
        'helper_command_available_after_review': bool(report.get('helper_command_after_review')),
    }


def build_report(root: Path = ROOT, env=None):
    env = os.environ if env is None else env
    root = Path(root)
    paths = _paths_for_root(root)
    issues = []
    visible_ok = []
    node_path = shutil.which('node')
    python_path = shutil.which('python3')
    report_parent = paths['report'].parent
    evidence = {
        'root': str(root),
        'mode': 'deterministic_primary_human_paced_backup',
        'legacy_human_paced_enabled': str(env.get('BASELANE_ENABLE_LEGACY_HUMAN_PACED', '0')).strip().lower() in {'1', 'true', 'yes', 'on'},
        'human_paced_backup_enabled': str(env.get('BASELANE_ENABLE_HUMAN_PACED_BACKUP', '1')).strip().lower() not in {'', '0', 'false', 'no', 'off'},
        'credentials_inspected': False,
        'report_path': str(paths['report']),
        'report_parent_exists': report_parent.exists(),
        'report_parent_writable': report_parent.exists() and os.access(report_parent, os.W_OK),
        'auth_preflight': _path_state(paths['auth_preflight']),
        'export_script': _path_state(paths['export_script']),
        'default_sync_script': _path_state(paths['default_sync_script']),
        'split_script': _path_state(paths['split_script']),
        'node_available': node_path is not None,
        'python3_available': python_path is not None,
        'cdp_target_count': len(CDP_TARGETS),
        'cdp_target_urls': _target_urls(),
        'base_cdp_version_url_present': bool(str(env.get('BASELANE_CDP_VERSION_URL', '')).strip()),
        'login_wait_url_configured': str(env.get('BASELANE_LOGIN_WAIT_URL', 'https://app.baselane.com/transactions')),
        'login_wait_timeout_ms': str(env.get('BASELANE_LOGIN_WAIT_TIMEOUT_MS', '180000')),
        'login_wait_ms': str(env.get('BASELANE_LOGIN_WAIT_MS', '15000')),
        'force_login_default': str(env.get('BASELANE_FORCE_LOGIN', '0')),
        'human_paced_force_login_override': str(env.get('BASELANE_HUMAN_PACED_FORCE_LOGIN', '0')),
        'cdp_probe_attempted': False,
        'login_subprocess_attempted': False,
        'human_pause_attempted': False,
        'export_subprocess_attempted': False,
        'split_subprocess_attempted': False,
        'report_write_attempted': False,
    }

    if not evidence['node_available']:
        issues.append('Node.js binary is not available for Baselane human-paced CDP scripts')
    if not evidence['python3_available']:
        issues.append('python3 binary is not available for the Baselane split step')
    if not evidence['report_parent_exists']:
        issues.append(f'Baselane human-paced sync report parent is missing: {report_parent}')
    elif not evidence['report_parent_writable']:
        issues.append(f'Baselane human-paced sync report parent is not writable: {report_parent}')
    if evidence['legacy_human_paced_enabled']:
        dependency_labels = ('auth_preflight', 'export_script', 'split_script')
    elif evidence['human_paced_backup_enabled']:
        dependency_labels = ('default_sync_script', 'auth_preflight', 'export_script', 'split_script')
    else:
        dependency_labels = ('default_sync_script',)
    for label in dependency_labels:
        state = evidence[label]
        if not state['exists']:
            issues.append(f'Required Baselane sync dependency is missing: {state["path"]}')
        elif not state['readable']:
            issues.append(f'Required Baselane sync dependency is not readable: {state["path"]}')

    if not issues:
        visible_ok.append(
            'OK Baselane sync compatibility entrypoint uses deterministic primary with human-paced backup: '
            f'default_sync={evidence["default_sync_script"]["readable"]} '
            f'human_paced_backup={evidence["human_paced_backup_enabled"]} '
            f'legacy_only={evidence["legacy_human_paced_enabled"]}'
        )
        visible_ok.append(
            'OK Baselane human-paced CDP sync diagnostic: '
            'no secrets read, CDP probe, subprocess, human pause, report write, restart, sudo, OAuth, or helper command'
        )

    classification = 'baselane-sync-cdp-human-paced-review' if issues else 'ok'
    classified_issues = classified_issue_records(issues, evidence, classification)
    fields = remediation_fields(classification)
    report = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'status': 'BASELANE_SYNC_CDP_HUMAN_PACED_REVIEW' if issues else 'NO_REPLY',
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
    parser = argparse.ArgumentParser(description='Run or inspect the human-paced Baselane CDP sync')
    parser.add_argument('--json', action='store_true', help='Emit a read-only diagnostic report and do not run sync')
    parser.add_argument('--root', default=str(ROOT), help='Workspace root to inspect for --json')
    return parser.parse_args(argv)


def main(argv=None, stdout: TextIO | None = None):
    args = parse_args(argv)
    if args.json:
        report = build_report(root=Path(args.root))
        print(json.dumps(report, indent=2, sort_keys=True), file=stdout or sys.stdout)
        return 0 if report['status'] == 'NO_REPLY' else 1

    try:
        code = run()
    except Exception as e:
        write_report({
            'status': 'failed',
            'reason': str(e),
            'mode': 'deterministic_primary_human_paced_backup'
        })
        print(f'FAILED: {e}', file=stdout or sys.stdout)
        return 1
    return code


if __name__ == '__main__':
    raise SystemExit(main())
