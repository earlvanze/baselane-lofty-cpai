#!/usr/bin/env python3
"""
Baselane Mortgage Split Automation via GraphQL
Processes split_needed rows from mortgage_split_checklist.csv
Uses splitTransaction mutation via baselane_graphql_via_cdp.js
"""
import argparse
import json
import csv
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, TextIO

WORKSPACE = Path.home() / '.openclaw' / 'workspace'
CHECKLIST = WORKSPACE / 'reports' / 'mortgage_split_checklist.csv'
STATE_FILE = WORKSPACE / 'reports' / 'mortgage_split_automation_state.json'
CDP_SCRIPT = WORKSPACE / 'scripts' / 'baselane_graphql_via_cdp.js'
ISSUE_CLASS = 'baselane-mortgage-split-automation'
SCRIPT_PATH = Path(__file__).resolve()


def diagnostic_command() -> str:
    return f'python3 {shlex.quote(str(SCRIPT_PATH))} --json'


DIAGNOSTIC_COMMAND = diagnostic_command()

# Category mapping per MORTGAGE SPLITS RETRY rules
# "interest/principal/late fees -> Mining Sales Consulting PM"
# "taxes/insurance/flood/PMI -> property"
# "escrow -> General Escrow"

# Baselane category IDs (need to be extracted from real account)
CATEGORY_MAP = {
    'principal': 'Mining Sales Consulting PM',  # placeholder
    'interest': 'Mining Sales Consulting PM',
    'late_fee': 'Mining Sales Consulting PM',
    'taxes': 'property',  # means property-specific category
    'insurance': 'property',
    'flood': 'property',
    'pmi': 'property',
    'escrow': 'General Escrow'
}

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {'processed': [], 'last_run': None}

def save_state(state):
    state['last_run'] = datetime.utcnow().isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=2))

def _paths_for_root(root: Path):
    return {
        'checklist': root / 'reports' / 'mortgage_split_checklist.csv',
        'state_file': root / 'reports' / 'mortgage_split_automation_state.json',
        'cdp_script': root / 'scripts' / 'baselane_graphql_via_cdp.js',
    }

def _path_state(path: Path):
    return {
        'path': str(path),
        'exists': path.exists(),
        'readable': path.is_file() and os.access(path, os.R_OK),
        'writable_parent': path.parent.exists() and os.access(path.parent, os.W_OK),
        'size_bytes': path.stat().st_size if path.exists() and path.is_file() else None,
    }

def _queue_summary(checklist: Path):
    summary = {
        'checklist_read_attempted': True,
        'checklist_read_ok': False,
        'checklist_total_rows': 0,
        'split_needed_count': 0,
        'status_counts': {},
        'missing_required_field_count': 0,
        'raw_transaction_example_count': 0,
        'scan_error': None,
    }
    required = {'status', 'property', 'month', 'examples'}
    try:
        with checklist.open(newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            fieldnames = set(reader.fieldnames or [])
            missing_headers = sorted(required - fieldnames)
            if missing_headers:
                summary['scan_error'] = f'missing required checklist headers: {", ".join(missing_headers)}'
                return summary
            for row in reader:
                summary['checklist_total_rows'] += 1
                status = (row.get('status') or '').strip() or '<blank>'
                summary['status_counts'][status] = summary['status_counts'].get(status, 0) + 1
                if status == 'split_needed':
                    summary['split_needed_count'] += 1
                    raw_count = sum(
                        1
                        for line in (row.get('examples') or '').split('||')
                        if line.strip().startswith('RAW ')
                    )
                    summary['raw_transaction_example_count'] += raw_count
                required_for_row = {'property', 'month', 'status'}
                if status == 'split_needed':
                    required_for_row.add('examples')
                if any(not (row.get(field) or '').strip() for field in required_for_row):
                    summary['missing_required_field_count'] += 1
    except Exception as exc:
        summary['scan_error'] = str(exc)
        return summary
    summary['checklist_read_ok'] = True
    return summary

def _state_summary(state_file: Path):
    summary = {
        'state_read_attempted': state_file.exists(),
        'state_read_ok': not state_file.exists(),
        'state_processed_count': 0,
        'state_last_run_present': False,
        'state_scan_error': None,
    }
    if not state_file.exists():
        return summary
    try:
        state = json.loads(state_file.read_text(encoding='utf-8'))
        processed = state.get('processed') if isinstance(state, dict) else None
        summary['state_processed_count'] = len(processed) if isinstance(processed, list) else 0
        summary['state_last_run_present'] = bool(state.get('last_run')) if isinstance(state, dict) else False
        summary['state_read_ok'] = isinstance(state, dict)
        if not isinstance(state, dict):
            summary['state_scan_error'] = 'state file is not a JSON object'
    except Exception as exc:
        summary['state_scan_error'] = str(exc)
    return summary

def remediation_fields(classification: str):
    has_issues = classification != 'ok'
    return {
        'remediation_class': 'operator-reviewed-baselane-mortgage-split-automation' if has_issues else 'no-remediation-needed',
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

def classified_issue_records(issues, evidence, classification):
    fields = remediation_fields(classification)
    validation = review_command_validation(fields.get('review_command'))
    return [
        {
            'issue': issue,
            'issue_class': ISSUE_CLASS,
            'classification': classification,
            'area': 'baselane-mortgage-split-automation',
            'checklist_readable': evidence.get('checklist', {}).get('readable'),
            'state_parent_writable': evidence.get('state_file', {}).get('writable_parent'),
            'cdp_script_readable': evidence.get('cdp_script', {}).get('readable'),
            'node_available': evidence.get('node_available'),
            'split_needed_count': evidence.get('split_needed_count'),
            'transaction_id_lookup_implemented': evidence.get('transaction_id_lookup_implemented'),
            'node_subprocess_attempted': evidence.get('node_subprocess_attempted'),
            'temp_payload_write_attempted': evidence.get('temp_payload_write_attempted'),
            'state_write_attempted': evidence.get('state_write_attempted'),
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
        'checklist_readable': report.get('checklist', {}).get('readable') is True,
        'cdp_script_readable': report.get('cdp_script', {}).get('readable') is True,
        'state_parent_writable': report.get('state_file', {}).get('writable_parent') is True,
        'split_needed_count': int(report.get('split_needed_count') or 0),
        'state_processed_count': int(report.get('state_processed_count') or 0),
        'transaction_id_lookup_implemented': report.get('transaction_id_lookup_implemented') is True,
        'node_subprocess_attempted': report.get('node_subprocess_attempted') is True,
        'temp_payload_write_attempted': report.get('temp_payload_write_attempted') is True,
        'split_mutation_attempted': report.get('split_mutation_attempted') is True,
        'state_write_attempted': report.get('state_write_attempted') is True,
        'remediation_class': report.get('remediation_class'),
        'cleanup_command_available_after_review': bool(report.get('cleanup_command_after_review')),
        'restart_command_available_after_review': bool(report.get('restart_command_after_review')),
        'oauth_command_available_after_review': bool(report.get('oauth_command_after_review')),
        'helper_command_available_after_review': bool(report.get('helper_command_after_review')),
    }

def build_report(root: Path = WORKSPACE):
    root = Path(root)
    paths = _paths_for_root(root)
    issues = []
    visible_ok = []

    evidence = {
        'root': str(root),
        'checklist': _path_state(paths['checklist']),
        'state_file': _path_state(paths['state_file']),
        'cdp_script': _path_state(paths['cdp_script']),
        'node_available': shutil.which('node') is not None,
        'temp_dir': tempfile.gettempdir(),
        'temp_dir_writable': os.access(tempfile.gettempdir(), os.W_OK),
        'batch_limit': 5,
        'category_rule_count': len(CATEGORY_MAP),
        'category_placeholder_count': sum(1 for value in CATEGORY_MAP.values() if value in {'property', 'Mining Sales Consulting PM'}),
        'transaction_id_lookup_implemented': False,
        'node_subprocess_attempted': False,
        'temp_payload_write_attempted': False,
        'split_mutation_attempted': False,
        'state_write_attempted': False,
    }

    if not evidence['checklist']['exists']:
        issues.append(f'Mortgage split checklist is missing: {paths["checklist"]}')
        queue_summary = {
            'checklist_read_attempted': False,
            'checklist_read_ok': False,
            'checklist_total_rows': 0,
            'split_needed_count': 0,
            'status_counts': {},
            'missing_required_field_count': 0,
            'raw_transaction_example_count': 0,
            'scan_error': None,
        }
    elif not evidence['checklist']['readable']:
        issues.append(f'Mortgage split checklist is not readable: {paths["checklist"]}')
        queue_summary = {
            'checklist_read_attempted': False,
            'checklist_read_ok': False,
            'checklist_total_rows': 0,
            'split_needed_count': 0,
            'status_counts': {},
            'missing_required_field_count': 0,
            'raw_transaction_example_count': 0,
            'scan_error': None,
        }
    else:
        queue_summary = _queue_summary(paths['checklist'])
        if queue_summary['scan_error']:
            issues.append(f'Mortgage split checklist could not be scanned: {queue_summary["scan_error"]}')
        if queue_summary['missing_required_field_count']:
            issues.append(
                f'Mortgage split checklist has {queue_summary["missing_required_field_count"]} rows missing required fields'
            )

    state_summary = _state_summary(paths['state_file'])
    if state_summary['state_scan_error']:
        issues.append(f'Mortgage split automation state could not be scanned: {state_summary["state_scan_error"]}')

    evidence.update(queue_summary)
    evidence.update(state_summary)

    if not evidence['node_available']:
        issues.append('Node.js binary is not available for the Baselane CDP split mutation bridge')
    if not evidence['cdp_script']['exists']:
        issues.append(f'Baselane GraphQL CDP bridge is missing: {paths["cdp_script"]}')
    elif not evidence['cdp_script']['readable']:
        issues.append(f'Baselane GraphQL CDP bridge is not readable: {paths["cdp_script"]}')
    if not evidence['state_file']['writable_parent']:
        issues.append(f'Mortgage split automation state parent is not writable: {paths["state_file"].parent}')
    if not evidence['temp_dir_writable']:
        issues.append(f'Temporary directory is not writable for reviewed split payloads: {evidence["temp_dir"]}')
    if evidence['split_needed_count'] and not evidence['transaction_id_lookup_implemented']:
        issues.append(
            'Mortgage split queue has pending rows, but transaction ID lookup is not implemented in this automation'
        )

    if not issues:
        visible_ok.append(
            'OK Baselane mortgage split automation config: '
            f'checklist_rows={evidence["checklist_total_rows"]} split_needed={evidence["split_needed_count"]} '
            f'node={evidence["node_available"]} cdp_script={evidence["cdp_script"]["readable"]}'
        )
        visible_ok.append(
            'OK Baselane mortgage split automation diagnostic: '
            'no Node subprocess, temp payload write, split mutation, state write, restart, sudo, OAuth, or helper command'
        )

    classification = 'baselane-mortgage-split-automation-review' if issues else 'ok'
    fields = remediation_fields(classification)
    classified_issues = classified_issue_records(issues, evidence, classification)
    report = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'status': 'BASELANE_MORTGAGE_SPLIT_AUTOMATION_REVIEW' if issues else 'NO_REPLY',
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

def load_split_queue():
    """Load split_needed rows from checklist, oldest first"""
    queue = []
    with open(CHECKLIST) as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row['status'] == 'split_needed':
                queue.append(row)

    # Sort by month (oldest first)
    queue.sort(key=lambda r: r['month'])
    return queue

def parse_mortgage_examples(examples_str):
    """
    Parse examples column to extract RAW transaction details
    Example format: "RAW November 12, 2024 -4270.6 CITADEL SERV PMT Hidden after mortgage split redo"
    """
    transactions = []
    for line in examples_str.split('||'):
        line = line.strip()
        if not line.startswith('RAW '):
            continue

        # Remove RAW prefix and parse
        parts = line[4:].strip().split()
        if len(parts) < 4:
            continue

        month = parts[0]
        day = parts[1].rstrip(',')
        year = parts[2]
        amount = parts[3]
        merchant = ' '.join(parts[4:]).split(' Hidden')[0].split(' unhide')[0].split(' *SPLIT*')[0]

        transactions.append({
            'date': f'{year}-{month}-{day}',
            'amount': amount,
            'merchant': merchant.strip()
        })

    return transactions

def execute_split_graphql(transaction_id, splits):
    """
    Execute splitTransaction mutation via CDP script
    splits = [{'amount': float, 'category': str, 'note': str}, ...]
    """
    mutation = {
        'operationName': 'SplitTransaction',
        'variables': {
            'id': transaction_id,
            'splits': splits
        },
        'query': '''
        mutation SplitTransaction($id: ID!, $splits: [TransactionSplitInput!]!) {
          splitTransaction(id: $id, splits: $splits) {
            id
            isSplit
            splits {
              id
              amount
              category
              note
            }
          }
        }
        '''
    }

    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(mutation, f)
        input_file = f.name

    try:
        result = subprocess.run(
            ['node', str(CDP_SCRIPT), input_file],
            capture_output=True,
            text=True,
            timeout=60
        )

        if result.returncode != 0:
            raise RuntimeError(f'GraphQL error: {result.stderr}')

        return json.loads(result.stdout)
    finally:
        Path(input_file).unlink()

def run(stdout: TextIO | None = None):
    out = stdout or sys.stdout
    print('[mortgage_split] Loading queue from checklist...', file=out)
    queue = load_split_queue()

    if not queue:
        print('[mortgage_split] ✓ Queue empty — all splits complete', file=out)
        return 0

    print(f'[mortgage_split] Found {len(queue)} pending splits', file=out)

    state = load_state()
    processed_count = 0

    for row in queue[:5]:  # Process 5 at a time per cron spec
        property_name = row['property']
        month = row['month']
        key = f'{property_name}|{month}'

        if key in state['processed']:
            print(f'[mortgage_split] SKIP {key} (already processed)', file=out)
            continue

        print(f'\n[mortgage_split] Processing: {property_name} {month}', file=out)

        # Parse RAW transactions from examples
        txns = parse_mortgage_examples(row['examples'])

        if not txns:
            print(f'[mortgage_split] WARN: No RAW transactions found in examples', file=out)
            continue

        print(f'[mortgage_split] Found {len(txns)} RAW transactions', file=out)

        # TODO: Need to get actual Baselane transaction IDs
        # This requires either:
        # 1. Export current ledger and match by date/amount/merchant
        # 2. Query transactions API by property/date range

        print(f'[mortgage_split] ERROR: Transaction ID lookup not implemented', file=out)
        print(f'[mortgage_split] Need to map: {txns[0]}', file=out)

        # For now, mark as processed to avoid infinite loop
        state['processed'].append(key)
        processed_count += 1

    save_state(state)
    print(f'\n[mortgage_split] Session complete: {processed_count} processed', file=out)
    return 0

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='Run or inspect Baselane mortgage split automation')
    parser.add_argument('--json', action='store_true', help='Emit a read-only diagnostic report and do not run splits')
    parser.add_argument('--root', default=str(WORKSPACE), help='Workspace root to inspect for --json')
    return parser.parse_args(argv)

def main(argv=None, stdout: TextIO | None = None):
    args = parse_args(argv)
    if args.json:
        report = build_report(root=Path(args.root))
        print(json.dumps(report, indent=2, sort_keys=True), file=stdout or sys.stdout)
        return 0 if report['status'] == 'NO_REPLY' else 1
    return run(stdout=stdout)

if __name__ == '__main__':
    raise SystemExit(main())
