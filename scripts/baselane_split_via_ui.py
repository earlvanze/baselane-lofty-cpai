#!/usr/bin/env python3
"""
Baselane mortgage split automation via CDP UI interaction.
Uses browser tool with chrome-relay profile for reliable selector targeting.
"""
from __future__ import annotations

import argparse
import csv
import json
import shlex
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_CSV_PATH = Path.home() / '.openclaw/workspace/reports/mortgage_split_checklist.csv'
SCRIPT_PATH = Path(__file__).resolve()
STATUS_OK = 'NO_REPLY'
STATUS_REVIEW = 'BASELANE_SPLIT_VIA_UI_REVIEW'
CLASS_OK = 'ok'
CLASS_REVIEW = 'baselane-split-via-ui-review'
COMPONENT_FIELDS = ('principal', 'interest', 'taxes', 'insurance', 'water', 'other')


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def diagnostic_command(script_path: Path | None = None, csv_path: Path | str = DEFAULT_CSV_PATH) -> str:
    script = SCRIPT_PATH if script_path is None else Path(script_path)
    return ' '.join([
        'python3',
        shlex.quote(str(script)),
        '--csv-path',
        shlex.quote(str(csv_path)),
        '--json',
    ])


def review_command_validation(
    command: object | None = None,
    script_path: Path | None = None,
    csv_path: Path | str = DEFAULT_CSV_PATH,
) -> dict[str, Any]:
    expected_script = (SCRIPT_PATH if script_path is None else Path(script_path)).resolve()
    expected_csv = Path(csv_path)
    command_text = diagnostic_command(expected_script, expected_csv) if command is None else str(command)
    parse_issue = None
    try:
        parts = shlex.split(command_text)
    except ValueError as exc:
        parts = []
        parse_issue = str(exc)

    python3_present = bool(parts and parts[0] == 'python3')
    script_path_present = str(expected_script) in parts
    csv_flag_present = '--csv-path' in parts
    csv_path_present = str(expected_csv) in parts
    json_flag_present = '--json' in parts
    script_exists = expected_script.exists()
    script_is_file = expected_script.is_file()

    issues: list[str] = []
    if parse_issue:
        issues.append(f'review command is not shell-parseable: {parse_issue}')
    if not python3_present:
        issues.append('review command must start with python3')
    if not script_path_present:
        issues.append(f'review command missing script path: {expected_script}')
    if not csv_flag_present:
        issues.append('review command missing --csv-path')
    if not csv_path_present:
        issues.append(f'review command missing CSV path: {expected_csv}')
    if not json_flag_present:
        issues.append('review command missing --json')
    for flag in ('--write', '--delete', '--sync', '--restart', '--apply'):
        if flag in parts:
            issues.append(f'review command must not include {flag}')
    if not script_exists:
        issues.append(f'review command path missing: {expected_script}')
    elif not script_is_file:
        issues.append(f'review command path is not a file: {expected_script}')

    return {
        'command': command_text,
        'expected_script_path': str(expected_script),
        'expected_csv_path': str(expected_csv),
        'path': str(expected_script),
        'path_exists': script_exists,
        'script_exists': script_exists,
        'script_is_file': script_is_file,
        'python3_present': python3_present,
        'script_path_present': script_path_present,
        'csv_flag_present': csv_flag_present,
        'csv_path_present': csv_path_present,
        'json_flag_present': json_flag_present,
        'parse_issue': parse_issue,
        'status': 'valid' if not issues else 'invalid',
        'valid': not issues,
        'requires_executable': False,
        'issue': issues[0] if issues else None,
        'issues': issues,
    }


def issue_record(message: str, csv_path: Path, script_path: Path | None = None) -> dict[str, Any]:
    script = SCRIPT_PATH if script_path is None else Path(script_path)
    command = diagnostic_command(script, csv_path)
    validation = review_command_validation(command, script, csv_path)
    return {
        'title': 'Baselane split-via-UI review',
        'issue': message,
        'issue_class': CLASS_REVIEW,
        'classification': CLASS_REVIEW,
        'requires_operator_approval': True,
        'requires_interactive_sudo': False,
        'requires_interactive_oauth': False,
        'safe_to_run_automatically': False,
        'review_command': command,
        'review_command_safe_to_run_automatically': True,
        'review_command_valid': validation['valid'],
        'review_command_validation': validation,
        'command': None,
        'cleanup_command_after_review': None,
        'restart_command_after_review': None,
        'oauth_command_after_review': None,
    }


def summarize_issues(records: list[dict[str, Any]]) -> dict[str, Any]:
    class_counts = Counter(record.get('issue_class', CLASS_REVIEW) for record in records)
    route_counts = Counter(record.get('classification', CLASS_REVIEW) for record in records)
    validation_issues = [
        issue
        for record in records
        for issue in ((record.get('review_command_validation') or {}).get('issues') or [])
    ]
    safe_count = sum(1 for record in records if record.get('review_command_safe_to_run_automatically'))
    valid_count = sum(1 for record in records if record.get('review_command_valid'))
    return {
        'total': len(records),
        'total_count': len(records),
        'issue_count': len(records),
        'covered_count': len(records),
        'uncovered_count': 0,
        'classes': sorted(class_counts),
        'class_counts': dict(sorted(class_counts.items())),
        'issue_class_counts': dict(sorted(class_counts.items())),
        'route_classification_counts': dict(sorted(route_counts.items())),
        'approval_required_count': sum(1 for record in records if record.get('requires_operator_approval')),
        'requires_operator_approval_count': sum(1 for record in records if record.get('requires_operator_approval')),
        'requires_interactive_sudo_count': sum(1 for record in records if record.get('requires_interactive_sudo')),
        'requires_interactive_oauth_count': sum(1 for record in records if record.get('requires_interactive_oauth')),
        'safe_review_command_count': safe_count,
        'valid_review_command_count': valid_count,
        'invalid_review_command_count': safe_count - valid_count,
        'review_command_validation_issues': validation_issues,
    }


def load_split_queue(csv_path: Path | str = DEFAULT_CSV_PATH) -> list[dict[str, Any]]:
    """Load pending splits from checklist CSV."""
    path = Path(csv_path)
    if not path.exists():
        print(f"ERROR: {path} not found", file=sys.stderr)
        sys.exit(1)

    splits = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get('status') == 'pending':
                splits.append({
                    'property': row['property'],
                    'month': row['month'],
                    'txn_id': row['transaction_id'],
                    'principal': float(row['principal']),
                    'interest': float(row['interest']),
                    'escrow': float(row['escrow']),
                    'taxes': float(row.get('taxes', 0) or 0),
                    'insurance': float(row.get('insurance', 0) or 0),
                    'water': float(row.get('water', 0) or 0),
                    'other': float(row.get('other', 0) or 0),
                })
    return splits


def build_ui_automation_plan(splits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Build Playwright-style automation steps for batch splits.

    Per MEMORY.md:
    - P&I -> Mining, Sales, Consulting, & PM
    - Escrow components -> property entity
    - Remainder -> General Escrow
    """
    plan = []
    for s in splits:
        plan.append({
            'action': 'navigate',
            'url': f"https://app.baselane.com/transactions?search={s['txn_id']}"
        })
        plan.append({
            'action': 'wait_for_selector',
            'selector': f"[data-transaction-id='{s['txn_id']}']"
        })
        plan.append({
            'action': 'click',
            'selector': f"[data-transaction-id='{s['txn_id']}'] button[aria-label='Split']"
        })
        plan.append({
            'action': 'wait_for_selector',
            'selector': "[data-testid='split-dialog']"
        })

        # Add split rows
        components = []
        if s['principal'] != 0:
            components.append(('Principal', s['principal'], 'Mortgage Principal Payments'))
        if s['interest'] != 0:
            components.append(('Interest', s['interest'], 'Mortgage Interest Payments'))
        if s['taxes'] != 0:
            components.append(('Taxes', s['taxes'], 'Taxes'))
        if s['insurance'] != 0:
            components.append(('Insurance', s['insurance'], 'Insurance'))
        if s['water'] != 0:
            components.append(('Water/Sewer', s['water'], 'Water & Sewer'))
        if s['other'] != 0:
            components.append(('Other Escrow', s['other'], 'Escrow Payments'))

        for label, amount, category in components:
            plan.append({
                'action': 'fill_split_row',
                'label': label,
                'amount': amount,
                'category': category
            })

        plan.append({
            'action': 'click',
            'selector': "[data-testid='split-dialog'] button[type='submit']"
        })
        plan.append({
            'action': 'wait_for_navigation',
            'timeout': 5000
        })

    return plan


def build_report(csv_path: Path | str = DEFAULT_CSV_PATH, script_path: Path | None = None) -> dict[str, Any]:
    path = Path(csv_path)
    script = SCRIPT_PATH if script_path is None else Path(script_path)
    records: list[dict[str, Any]] = []
    csv_exists = path.exists()
    csv_is_file = path.is_file()
    csv_read_attempted = False
    csv_parse_ok = False
    csv_error_present = False
    row_count = 0
    pending_count = 0
    nonpending_count = 0
    component_counts = {field: 0 for field in COMPONENT_FIELDS}
    total_component_count = 0
    total_component_amount_cents = 0

    if not csv_exists:
        records.append(issue_record('Baselane mortgage split checklist CSV is missing', path, script))
    elif not csv_is_file:
        records.append(issue_record('Baselane mortgage split checklist path is not a file', path, script))
    else:
        csv_read_attempted = True
        try:
            with open(path) as f:
                reader = csv.DictReader(f)
                required = {'status', 'property', 'month', 'transaction_id', 'principal', 'interest', 'escrow'}
                missing = sorted(required - set(reader.fieldnames or []))
                if missing:
                    raise ValueError(f'missing columns: {", ".join(missing)}')
                for row in reader:
                    row_count += 1
                    if row.get('status') != 'pending':
                        nonpending_count += 1
                        continue
                    pending_count += 1
                    for field in COMPONENT_FIELDS:
                        value = float(row.get(field, 0) or 0)
                        if value != 0:
                            component_counts[field] += 1
                            total_component_count += 1
                            total_component_amount_cents += int(round(abs(value) * 100))
            csv_parse_ok = True
        except Exception:
            csv_error_present = True
            records.append(issue_record('Baselane mortgage split checklist CSV could not be parsed', path, script))

    planned_steps_per_split = 6
    planned_ui_step_count = pending_count * planned_steps_per_split + total_component_count
    summary = summarize_issues(records)
    return {
        'status': STATUS_REVIEW if records else STATUS_OK,
        'classification': CLASS_REVIEW if records else CLASS_OK,
        'generated_at': now_iso(),
        'ok_state': not records,
        'ok': not records,
        'visible_ok': not records,
        'issues': [record['issue'] for record in records],
        'issue_count': len(records),
        'issue_classes': sorted({record['issue_class'] for record in records}),
        'classified_issues': records,
        'issue_records': records,
        'structured_issues': records,
        'classified_issue_summary': summary,
        'remediation': {
            'command': None,
            'cleanup_command_after_review': None,
            'restart_command_after_review': None,
            'oauth_command_after_review': None,
        },
        'approval_required_count': summary['approval_required_count'],
        'requires_operator_approval_count': summary['requires_operator_approval_count'],
        'requires_interactive_sudo_count': summary['requires_interactive_sudo_count'],
        'requires_interactive_oauth_count': summary['requires_interactive_oauth_count'],
        'safe_review_command_count': summary['safe_review_command_count'],
        'valid_review_command_count': summary['valid_review_command_count'],
        'invalid_review_command_count': summary['invalid_review_command_count'],
        'review_command_validation_issues': summary['review_command_validation_issues'],
        'csv_path': str(path),
        'csv_exists': csv_exists,
        'csv_is_file': csv_is_file,
        'csv_read_attempted': csv_read_attempted,
        'csv_parse_ok': csv_parse_ok,
        'csv_error_present': csv_error_present,
        'csv_row_count': row_count,
        'pending_split_count': pending_count,
        'nonpending_row_count': nonpending_count,
        'component_counts': component_counts,
        'total_component_count': total_component_count,
        'total_component_amount_cents': total_component_amount_cents,
        'planned_navigation_count': pending_count,
        'planned_split_button_click_count': pending_count,
        'planned_dialog_submit_count': pending_count,
        'planned_fill_split_row_count': total_component_count,
        'planned_ui_step_count': planned_ui_step_count,
        'planned_total_filesystem_mutation_count': 0,
        'file_read_attempted': csv_read_attempted,
        'file_write_attempted': False,
        'browser_launch_attempted': False,
        'cdp_connection_attempted': False,
        'navigation_attempted': False,
        'split_click_attempted': False,
        'dialog_submit_attempted': False,
        'baselane_mutation_attempted': False,
        'network_attempted': False,
        'subprocess_attempted': False,
        'delete_attempted': False,
        'sync_attempted': False,
        'restart_attempted': False,
        'property_names_included': False,
        'transaction_ids_included': False,
        'month_values_included': False,
        'row_content_included': False,
        'selectors_included': False,
        'plan_steps_included': False,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Build Baselane split-via-UI automation plans')
    parser.add_argument('--csv-path', default=str(DEFAULT_CSV_PATH), help='Mortgage split checklist CSV')
    parser.add_argument('--json', action='store_true', help='Emit an aggregate no-action diagnostic report')
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, stdout=None, stderr=None) -> int:
    if stdout is None:
        stdout = sys.stdout
    if stderr is None:
        stderr = sys.stderr
    args = parse_args(argv)
    csv_path = Path(args.csv_path)
    if args.json:
        print(json.dumps(build_report(csv_path), indent=2, sort_keys=True), file=stdout)
        return 0

    splits = load_split_queue(csv_path)
    print(f"Loaded {len(splits)} pending splits", file=stderr)

    plan = build_ui_automation_plan(splits)
    print(json.dumps(plan, indent=2), file=stdout)
    return 0


if __name__ == '__main__':
    sys.exit(main())
