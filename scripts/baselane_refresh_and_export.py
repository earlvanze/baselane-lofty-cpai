#!/usr/bin/env python3
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get('OPENCLAW_WORKSPACE_ROOT', Path(__file__).absolute().parents[1]))
SCRIPT_PATH = Path(__file__).resolve()
ISSUE_CLASS = 'baselane-refresh-and-export'
STATUS_OK = 'NO_REPLY'
STATUS_REVIEW = 'BASELANE_REFRESH_AND_EXPORT_REVIEW'
CLASS_OK = 'ok'
CLASS_REVIEW = 'baselane-refresh-and-export-review'


def paths_for_root(root: Path):
    return {
        'root': root,
        'session_script': root / 'scripts' / 'baselane_cdp_auth_recovery.py',
    }


def diagnostic_command(root: Path, script_path: Path | None = None) -> str:
    script_path = SCRIPT_PATH if script_path is None else script_path
    return f'python3 {shlex.quote(str(script_path))} --root {shlex.quote(str(root))} --json'


def review_command_validation(root: Path, command: object | None = None, script_path: Path | None = None):
    script_path = SCRIPT_PATH if script_path is None else script_path
    command = diagnostic_command(root, script_path=script_path) if command is None else command
    expected_script = str(script_path)
    expected_root = str(root)
    issues = []
    parts = []
    if not isinstance(command, str) or not command.strip():
        issues.append('review command is empty or not a string')
    else:
        try:
            parts = shlex.split(command)
        except ValueError as exc:
            issues.append(f'review command is not shell-parseable: {exc}')
    script_exists = script_path.exists()
    script_is_file = script_path.is_file()
    python3_present = bool(parts) and parts[0] == 'python3'
    script_path_present = expected_script in parts
    root_flag_present = '--root' in parts
    root_value_present = expected_root in parts
    json_flag_present = '--json' in parts
    if not python3_present:
        issues.append('review command must start with python3')
    if not script_path_present:
        issues.append(f'review command must include script path: {expected_script}')
    if not root_flag_present:
        issues.append('review command must include --root')
    if not root_value_present:
        issues.append(f'review command must include root path: {expected_root}')
    if not json_flag_present:
        issues.append('review command must include --json')
    if not script_exists:
        issues.append(f'review command script path does not exist: {expected_script}')
    elif not script_is_file:
        issues.append(f'review command script path is not a file: {expected_script}')
    return {
        'command': command,
        'expected_script_path': expected_script,
        'expected_root': expected_root,
        'script_exists': script_exists,
        'script_is_file': script_is_file,
        'path': expected_script,
        'path_exists': script_exists,
        'python3_present': python3_present,
        'script_path_present': script_path_present,
        'root_flag_present': root_flag_present,
        'root_value_present': root_value_present,
        'json_flag_present': json_flag_present,
        'requires_executable': False,
        'valid': not issues,
        'issues': issues,
        'issue': issues[0] if issues else None,
    }


def path_state(path: Path):
    exists = path.exists()
    is_file = path.is_file()
    return {
        'path': str(path),
        'exists': exists,
        'readable': is_file and os.access(path, os.R_OK),
        'executable': is_file and os.access(path, os.X_OK),
        'is_file': is_file,
        'is_dir': path.is_dir(),
        'size_bytes': path.stat().st_size if exists and is_file else None,
    }


def remediation_fields(root: Path, classification: str):
    has_issues = classification != CLASS_OK
    return {
        'remediation_class': 'operator-reviewed-baselane-refresh-and-export' if has_issues else 'no-remediation-needed',
        'requires_operator_approval': has_issues,
        'requires_interactive_sudo': False,
        'requires_interactive_oauth': False,
        'safe_to_run_automatically': not has_issues,
        'review_command': diagnostic_command(root),
        'review_command_safe_to_run_automatically': True,
        'cleanup_command_after_review': None,
        'restart_command_after_review': None,
        'oauth_command_after_review': None,
        'helper_command_after_review': None,
    }


def issue_record(issue: str, root: Path, evidence: dict, classification: str):
    fields = remediation_fields(root, classification)
    validation = review_command_validation(root, fields.get('review_command'))
    return {
        'issue': issue,
        'title': 'Baselane refresh/export launcher review',
        'issue_class': ISSUE_CLASS,
        'classification': classification,
        'area': 'baselane-refresh-export',
        'root': str(root),
        'python3_available': evidence.get('python3_available'),
        'venv_python_readable': None,
        'venv_python_executable': None,
        'session_script_readable': evidence.get('session_script', {}).get('readable'),
        'requires_operator_approval': fields['requires_operator_approval'],
        'requires_interactive_sudo': fields['requires_interactive_sudo'],
        'requires_interactive_oauth': fields['requires_interactive_oauth'],
        'safe_to_run_automatically': fields['safe_to_run_automatically'],
        'review_command': fields['review_command'],
        'review_command_safe_to_run_automatically': fields['review_command_safe_to_run_automatically'],
        'review_command_valid': validation['valid'],
        'review_command_validation': validation,
        'cleanup_command_after_review': None,
        'restart_command_after_review': None,
        'oauth_command_after_review': None,
        'helper_command_after_review': None,
    }


def classified_issue_summary(records):
    safe = [r for r in records if r.get('review_command_safe_to_run_automatically')]
    valid = [r for r in safe if r.get('review_command_valid')]
    invalid = [r for r in safe if not r.get('review_command_valid')]
    validation_issues = []
    for record in invalid:
        validation_issues.extend(record.get('review_command_validation', {}).get('issues') or [])
    return {
        'class_counts': {ISSUE_CLASS: len(records)} if records else {},
        'classification_counts': {CLASS_REVIEW: len(records)} if records else {},
        'requires_operator_approval_count': sum(1 for r in records if r.get('requires_operator_approval')),
        'approval_required_count': sum(1 for r in records if r.get('requires_operator_approval')),
        'review_required_count': len(records),
        'requires_interactive_sudo_count': sum(1 for r in records if r.get('requires_interactive_sudo')),
        'requires_interactive_oauth_count': sum(1 for r in records if r.get('requires_interactive_oauth')),
        'safe_review_command_count': len(safe),
        'valid_review_command_count': len(valid),
        'invalid_review_command_count': len(invalid),
        'review_command_validation_issues': validation_issues,
        'python3_available': None,
        'session_script_readable': None,
        'session_subprocess_attempted': False,
        'network_attempted': False,
    }


def build_report(root: Path = ROOT):
    paths = paths_for_root(root)
    evidence = {
        'root': path_state(paths['root']),
        'session_script': path_state(paths['session_script']),
        'python3_available': shutil.which('python3') is not None,
        'session_subprocess_attempted': False,
        'deterministic_session_attempted': False,
        'browser_launch_attempted': False,
        'cdp_probe_attempted': False,
        'bitwarden_subprocess_attempted': False,
        'refresh_export_subprocess_attempted': False,
        'split_subprocess_attempted': False,
        'report_write_attempted': False,
        'state_write_attempted': False,
        'file_write_attempted': False,
        'directory_create_attempted': False,
        'delete_attempted': False,
        'sync_attempted': False,
        'restart_attempted': False,
        'network_attempted': False,
    }
    issues = []
    if not evidence['root']['is_dir']:
        issues.append(f'Workspace root is missing or not a directory: {root}')
    if not evidence['python3_available']:
        issues.append('python3 binary is not available on PATH')
    if not evidence['session_script']['readable']:
        issues.append(f'Baselane human-session preflight script is missing or unreadable: {paths["session_script"]}')
    classification = CLASS_OK if not issues else CLASS_REVIEW
    status = STATUS_OK if not issues else STATUS_REVIEW
    records = [issue_record(issue, root, evidence, classification) for issue in issues]
    summary = classified_issue_summary(records)
    summary['python3_available'] = evidence['python3_available']
    summary['session_script_readable'] = evidence['session_script']['readable']
    report = {
        'status': status,
        'classification': classification,
        'ok_state': not issues,
        'ok': [] if issues else ['Baselane refresh/export launcher is locally ready'],
        'visible_ok': [] if issues else ['Baselane refresh/export launcher is locally ready'],
        'issues': issues,
        'issue_count': len(issues),
        'ok_count': 0 if issues else 1,
        'advisory_count': 0,
        'review_required_count': len(records),
        'approval_required_count': summary['approval_required_count'],
        'requires_operator_approval_count': summary['requires_operator_approval_count'],
        'issue_classes': [ISSUE_CLASS] if issues else [],
        'classified_issues': records,
        'issue_records': records,
        'structured_issues': records,
        'classified_issue_summary': summary,
        'safe_review_command_count': summary['safe_review_command_count'],
        'valid_review_command_count': summary['valid_review_command_count'],
        'invalid_review_command_count': summary['invalid_review_command_count'],
        'review_command_validation_issues': summary['review_command_validation_issues'],
        **evidence,
        **remediation_fields(root, classification),
    }
    return report


def run(root: Path = ROOT) -> int:
    # Compatibility wrapper: verify a human-provided session. The caller may
    # then run the deterministic export pipeline separately.
    paths = paths_for_root(root)
    return subprocess.call([sys.executable, str(paths['session_script'])])


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='Baselane refresh/export compatibility launcher.')
    parser.add_argument('--root', default=str(ROOT), help='OpenClaw workspace root containing the deterministic session script')
    parser.add_argument('--json', action='store_true', help='Emit a no-action dashboard diagnostic instead of launching the session')
    return parser.parse_args(argv)


def main(argv=None, stdout=sys.stdout):
    args = parse_args(argv)
    root = Path(args.root)
    if args.json:
        print(json.dumps(build_report(root=root), indent=2), file=stdout)
        return 0
    return run(root=root)


if __name__ == '__main__':
    raise SystemExit(main())
