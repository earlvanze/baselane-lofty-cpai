#!/usr/bin/env python3
"""
Build a durable ECO property context table for discord-public.
Outputs under workspace-discord-public:
- eco_property_context_table.csv
- eco_property_context_table.md
- eco_property_context_table.json
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

ROOT = Path(os.environ.get('DISCORD_PUBLIC_WORKSPACE_ROOT', str(Path.home() / '.openclaw' / 'workspace-discord-public')))
PROFILE_URL = 'https://www.loftyassist.com/api/profiles/eco'
PROPERTY_URL = 'https://www.loftyassist.com/api/properties/{slug}'
SCRIPT_PATH = Path(__file__).resolve()
ISSUE_CLASS = 'loftyassist-eco-table'


def iso(ts: Any) -> str:
    if ts is None:
        return ''
    if isinstance(ts, (int, float)):
        # heuristics: ms epoch if very large
        if ts > 10_000_000_000:
            ts = ts / 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d')
    if isinstance(ts, str):
        s = ts.strip()
        if not s:
            return ''
        # pass through already-date-like strings
        m = re.match(r'^(\d{4}-\d{2}-\d{2})', s)
        if m:
            return m.group(1)
        return s
    return str(ts)


def first_match(obj: Any, patterns: list[str]) -> Any:
    pats = [p.lower() for p in patterns]

    def rec(x: Any) -> Any:
        if isinstance(x, dict):
            # key-first
            for k, v in x.items():
                lk = k.lower()
                if any(p in lk for p in pats):
                    if isinstance(v, (str, int, float, bool)) and str(v).strip() != '':
                        return v
            for v in x.values():
                y = rec(v)
                if y is not None:
                    return y
        elif isinstance(x, list):
            for v in x:
                y = rec(v)
                if y is not None:
                    return y
        return None

    return rec(obj)


def occupancy_label(prop: dict[str, Any]) -> str:
    custom = (prop.get('custom_occupancy') or '').strip()
    if custom:
        return custom
    occupied = prop.get('is_occupied')
    if occupied is True:
        return 'Occupied'
    if occupied is False:
        return 'Vacant/Unknown'
    return 'Unknown'


def fetch_json(url: str, timeout: int = 40) -> Any:
    import requests

    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Build the ECO LoftyAssist property context table.')
    parser.add_argument('--out-dir', default=str(ROOT), help='Directory for generated ECO table artifacts')
    parser.add_argument('--json', action='store_true', help='Emit a read-only dashboard diagnostic and do not fetch or write')
    return parser.parse_args(argv)


def review_command(args: argparse.Namespace) -> str:
    parts = ['python3', str(SCRIPT_PATH), '--out-dir', str(args.out_dir), '--json']
    return ' '.join(shlex.quote(part) for part in parts)


def review_command_validation(command: object | None, args: argparse.Namespace) -> dict[str, Any]:
    command_text = '' if command is None else str(command)
    validation: dict[str, Any] = {
        'valid': False,
        'issues': [],
        'command': command_text,
        'parts': [],
        'script_path': str(SCRIPT_PATH),
        'script_exists': SCRIPT_PATH.exists(),
        'script_is_file': SCRIPT_PATH.is_file(),
        'json_flag_present': False,
    }
    try:
        parts = shlex.split(command_text)
    except ValueError as exc:
        validation['issues'].append(f'parse-error:{exc}')
        return validation

    validation['parts'] = parts
    validation['json_flag_present'] = '--json' in parts
    if len(parts) != 5:
        validation['issues'].append('unexpected-argument-count')
    if not parts or parts[0] != 'python3':
        validation['issues'].append('missing-python3')
    if len(parts) < 2 or Path(parts[1]).resolve() != SCRIPT_PATH:
        validation['issues'].append('unexpected-script-path')
    if '--out-dir' not in parts:
        validation['issues'].append('missing-out-dir-flag')
    else:
        idx = parts.index('--out-dir')
        if idx + 1 >= len(parts) or parts[idx + 1] != str(args.out_dir):
            validation['issues'].append('unexpected-out-dir')
    if '--json' not in parts:
        validation['issues'].append('missing-json-flag')
    for flag in ('--apply', '--write', '--force'):
        if flag in parts:
            validation['issues'].append(f'unexpected-{flag.lstrip("-")}-flag')
    if not SCRIPT_PATH.exists():
        validation['issues'].append('script-missing')
    elif not SCRIPT_PATH.is_file():
        validation['issues'].append('script-not-file')

    validation['valid'] = not validation['issues']
    return validation


def classified_summary(classified_issues: list[dict[str, Any]]) -> dict[str, Any]:
    class_counts: dict[str, int] = {}
    route_counts: dict[str, int] = {}
    validation_issues: list[str] = []
    for issue in classified_issues:
        issue_class = issue.get('issue_class')
        route = issue.get('route')
        if issue_class:
            class_counts[issue_class] = class_counts.get(issue_class, 0) + 1
        if route:
            route_counts[route] = route_counts.get(route, 0) + 1
        for item in (issue.get('review_command_validation') or {}).get('issues') or []:
            validation_issues.append(str(item))
    safe_count = sum(1 for issue in classified_issues if issue.get('review_command_safe_to_run_automatically'))
    valid_count = sum(1 for issue in classified_issues if issue.get('review_command_valid'))
    return {
        'total': len(classified_issues),
        'total_count': len(classified_issues),
        'classified_record_count': len(classified_issues),
        'class_counts': class_counts,
        'issue_class_counts': class_counts,
        'route_classification_counts': route_counts,
        'review_required_count': len(classified_issues),
        'approval_required_count': sum(1 for issue in classified_issues if issue.get('requires_operator_approval')),
        'requires_operator_approval_count': sum(1 for issue in classified_issues if issue.get('requires_operator_approval')),
        'interactive_sudo_count': sum(1 for issue in classified_issues if issue.get('requires_interactive_sudo')),
        'interactive_oauth_count': sum(1 for issue in classified_issues if issue.get('requires_interactive_oauth')),
        'requires_interactive_sudo_count': sum(1 for issue in classified_issues if issue.get('requires_interactive_sudo')),
        'requires_interactive_oauth_count': sum(1 for issue in classified_issues if issue.get('requires_interactive_oauth')),
        'safe_review_command_count': safe_count,
        'valid_review_command_count': valid_count,
        'invalid_review_command_count': safe_count - valid_count,
        'review_command_validation_issues': validation_issues,
    }


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    out_dir = Path(args.out_dir)
    output_paths = {
        'table_json': str(out_dir / 'eco_property_context_table.json'),
        'table_csv': str(out_dir / 'eco_property_context_table.csv'),
        'table_markdown': str(out_dir / 'eco_property_context_table.md'),
    }
    issues: list[str] = []
    requests_available = importlib.util.find_spec('requests') is not None
    if not requests_available:
        issues.append('Python dependency missing: requests')
    if out_dir.exists() and not out_dir.is_dir():
        issues.append(f'Output path exists but is not a directory: {out_dir}')
    elif not out_dir.exists() and not out_dir.parent.exists():
        issues.append(f'Output parent missing: {out_dir.parent}')
    elif not out_dir.exists() and not out_dir.parent.is_dir():
        issues.append(f'Output parent is not a directory: {out_dir.parent}')

    command = review_command(args)
    validation = review_command_validation(command, args)
    classified_issues: list[dict[str, Any]] = []
    for text in issues:
        classified_issues.append(
            {
                'issue_class': ISSUE_CLASS,
                'route': ISSUE_CLASS,
                'classification': 'loftyassist-eco-table-review',
                'severity': 'medium',
                'title': 'LoftyAssist ECO table preflight issue',
                'issue': text,
                'area': 'loftyassist-public-context',
                'out_dir': str(out_dir),
                'out_dir_exists': out_dir.exists(),
                'out_parent_exists': out_dir.parent.exists(),
                'requests_available': requests_available,
                'fetch_attempted': False,
                'network_attempted': False,
                'write_attempted': False,
                'directory_create_attempted': False,
                'row_build_attempted': False,
                'remediation_class': 'operator-reviewed-loftyassist-eco-table',
                'requires_operator_approval': True,
                'requires_interactive_sudo': False,
                'requires_interactive_oauth': False,
                'safe_to_run_automatically': False,
                'review_command': command,
                'review_command_safe_to_run_automatically': True,
                'review_command_valid': validation['valid'],
                'review_command_validation': validation,
                'cleanup_command_after_review': None,
                'restart_command_after_review': None,
                'oauth_command_after_review': None,
                'helper_command_after_review': None,
                'remediation': {
                    'command': None,
                    'review_command': command,
                    'review_command_validation': validation,
                },
            }
        )

    ok_state = not classified_issues
    ok = ['LoftyAssist ECO table diagnostic OK: local preflight passed'] if ok_state else []
    summary = classified_summary(classified_issues)
    report = {
        'status': 'NO_REPLY' if ok_state else 'LOFTYASSIST_ECO_TABLE_REVIEW',
        'classification': 'ok' if ok_state else 'loftyassist-eco-table-review',
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'source_profile_url': PROFILE_URL,
        'source_property_url_template': PROPERTY_URL,
        'out_dir': str(out_dir),
        'out_dir_exists': out_dir.exists(),
        'out_parent_exists': out_dir.parent.exists(),
        'out_parent_is_dir': out_dir.parent.is_dir(),
        'output_paths': output_paths,
        'requests_available': requests_available,
        'fetch_attempted': False,
        'network_attempted': False,
        'write_attempted': False,
        'directory_create_attempted': False,
        'row_build_attempted': False,
        'ok': ok,
        'issues': issues,
        'ok_state': ok_state,
        'visible_ok': ok,
        'ok_count': len(ok),
        'issue_count': len(classified_issues),
        'advisory_count': 0,
        'review_required_count': len(classified_issues),
        'approval_required_count': len(classified_issues),
        'requires_operator_approval_count': len(classified_issues),
        'interactive_sudo_count': 0,
        'interactive_oauth_count': 0,
        'requires_interactive_sudo_count': 0,
        'requires_interactive_oauth_count': 0,
        'issue_classes': sorted({issue['issue_class'] for issue in classified_issues}),
        'classified_issues': classified_issues,
        'classified_issue_summary': summary,
        'remediation_class': 'no-remediation-needed' if ok_state else 'operator-reviewed-loftyassist-eco-table',
        'requires_operator_approval': not ok_state,
        'requires_interactive_sudo': False,
        'requires_interactive_oauth': False,
        'safe_to_run_automatically': ok_state,
        'review_command': None if ok_state else command,
        'review_command_safe_to_run_automatically': not ok_state,
        'review_command_valid': None if ok_state else validation['valid'],
        'review_command_validation': None if ok_state else validation,
        'safe_review_command_count': summary['safe_review_command_count'],
        'valid_review_command_count': summary['valid_review_command_count'],
        'invalid_review_command_count': summary['invalid_review_command_count'],
        'review_command_validation_issues': summary['review_command_validation_issues'],
        'cleanup_command_after_review': None,
        'restart_command_after_review': None,
        'oauth_command_after_review': None,
        'helper_command_after_review': None,
        'remediation': {
            'command': None,
            'review_command': None if ok_state else command,
            'review_command_validation': None if ok_state else validation,
        },
    }
    report['classified_issue_summary'].update(
        {
            'classification': report['classification'],
            'route_classification': report['classification'],
            'ok_count': report['ok_count'],
            'issue_count': report['issue_count'],
            'visible_ok_count': len(report['visible_ok']),
            'requests_available': requests_available,
            'out_dir_exists': report['out_dir_exists'],
            'out_parent_exists': report['out_parent_exists'],
            'fetch_attempted': False,
            'network_attempted': False,
            'write_attempted': False,
            'directory_create_attempted': False,
            'row_build_attempted': False,
        }
    )
    return report


def run(out_dir: Path, stdout: TextIO = sys.stdout) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)

    profile = fetch_json(PROFILE_URL, timeout=40)

    managed = profile.get('propertiesManaged') or []

    rows: list[dict[str, Any]] = []
    for item in managed:
        prop = item.get('property') or {}
        slug = prop.get('slug')
        if not slug:
            continue

        detail = {}
        try:
            detail = fetch_json(PROPERTY_URL.format(slug=slug), timeout=40)
        except Exception:
            detail = {}

        dprop = detail.get('property') or {}

        # DSCR and debt maturity are often loan-scoped if present.
        active_loans = detail.get('activeLoans') or []
        loan_scope = {'activeLoans': active_loans, 'totalLoans': detail.get('totalLoans')}

        dscr = first_match(loan_scope, ['dscr']) or first_match(detail, ['dscr'])
        debt_maturity = (
            first_match(loan_scope, ['maturity', 'debt_maturity', 'maturity_date'])
            or first_match(detail, ['nextdebtmaturity', 'debtmaturity', 'maturity'])
        )
        last_distribution = first_match(
            detail,
            ['lastdistribution', 'distributiondate', 'distribution', 'lastpayout', 'lastdividend'],
        )

        row = {
            'slug': slug,
            'ticker': prop.get('assetUnit') or dprop.get('assetUnit') or '',
            'address': prop.get('address') or dprop.get('address') or '',
            'occupancy': occupancy_label(dprop or prop),
            'dscr': '' if dscr is None else str(dscr),
            'next_debt_maturity': iso(debt_maturity),
            'last_distribution': iso(last_distribution),
            'status': detail.get('status') or item.get('status') or '',
            'updated_date': dprop.get('updatedDate') or detail.get('updatedDate') or '',
            'source_profile': PROFILE_URL,
            'source_property': PROPERTY_URL.format(slug=slug),
        }
        rows.append(row)

    rows.sort(key=lambda r: (r['ticker'] or 'ZZZ', r['address'] or ''))

    # JSON
    (out_dir / 'eco_property_context_table.json').write_text(
        json.dumps(
            {
                'generated_at': datetime.now(timezone.utc).isoformat(),
                'profile': PROFILE_URL,
                'count': len(rows),
                'rows': rows,
            },
            indent=2,
        )
        + '\n',
        encoding='utf-8',
    )

    # CSV
    csv_fields = [
        'ticker',
        'slug',
        'address',
        'occupancy',
        'dscr',
        'next_debt_maturity',
        'last_distribution',
        'status',
        'updated_date',
        'source_property',
    ]
    with (out_dir / 'eco_property_context_table.csv').open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=csv_fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, '') for k in csv_fields})

    # Markdown
    lines = []
    lines.append('# ECO Property Context Table')
    lines.append('')
    lines.append(f'Generated: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}')
    lines.append(f'Profile source: {PROFILE_URL}')
    lines.append('')
    lines.append('Notes:')
    lines.append('- This table is the durable quick-reference for Discord investor Q&A.')
    lines.append('- If DSCR/debt/distribution fields are blank, LoftyAssist did not expose a clear value in the current API payload.')
    lines.append('- When blanks appear, supplement using latest Dropbox Public docs before answering.')
    lines.append('')
    lines.append('| Ticker | Property | Occupancy | DSCR | Next debt maturity | Last distribution |')
    lines.append('|---|---|---:|---:|---|---|')
    for r in rows:
        lines.append(
            f"| {r['ticker'] or '—'} | {r['address'] or r['slug']} | {r['occupancy'] or '—'} | {r['dscr'] or '—'} | {r['next_debt_maturity'] or '—'} | {r['last_distribution'] or '—'} |"
        )

    (out_dir / 'eco_property_context_table.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')

    stdout.write(f'Wrote {len(rows)} rows to {out_dir}\n')
    return 0


def main(argv: list[str] | None = None, stdout: TextIO = sys.stdout) -> int:
    args = parse_args(argv)
    if args.json:
        report = build_report(args)
        stdout.write(json.dumps(report, indent=2, sort_keys=True) + '\n')
        return 0 if report['ok_state'] else 1
    return run(Path(args.out_dir), stdout=stdout)


if __name__ == '__main__':
    raise SystemExit(main())
