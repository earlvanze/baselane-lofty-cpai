#!/usr/bin/env python3
"""
Baselane mortgage split automation.
Uses the createOrUpdateSplitTx GraphQL mutation discovered from HAR traffic.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import shlex
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# HAR file with auth cookies.
HAR_PATH = os.environ.get(
    'BASELANE_BATCH_SPLIT_HAR',
    str(Path.home() / 'Downloads' / 'mortgage_splits_app.baselane.com.har'),
)

# Property IDs
PROP_IDS = {
    '90 Madison Ave': 31525,
    '86 Madison Ave': 63162,
    '724 3rd Ave': 33594,
    'Mining, Sales, Consulting, and PM': 37648,
}

# Tag IDs from historical splits
TAG_PRINCIPAL = '20'
TAG_INTEREST = '11'
TAG_ESCROW = '130'
TAG_TAXES = '95'
TAG_INSURANCE = '65'
TAG_WATER = '104'

# Bank account ID (from HAR: 89680)
BANK_ACCOUNT_ID = 89680

SCRIPT_PATH = Path(__file__).resolve()
STATUS_OK = 'NO_REPLY'
STATUS_REVIEW = 'BASELANE_BATCH_SPLIT_REVIEW'
CLASS_OK = 'ok'
CLASS_REVIEW = 'baselane-batch-split-review'


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def diagnostic_command(script_path: Path | None = None, har_path: Path | str = HAR_PATH) -> str:
    script = SCRIPT_PATH if script_path is None else Path(script_path)
    return ' '.join([
        'python3',
        shlex.quote(str(script)),
        '--har-path',
        shlex.quote(str(har_path)),
        '--json',
    ])


def review_command_validation(
    command: object | None = None,
    script_path: Path | None = None,
    har_path: Path | str = HAR_PATH,
) -> dict[str, Any]:
    expected_script = (SCRIPT_PATH if script_path is None else Path(script_path)).resolve()
    expected_har = Path(har_path)
    command_text = diagnostic_command(expected_script, expected_har) if command is None else str(command)
    parse_issue = None
    try:
        parts = shlex.split(command_text)
    except ValueError as exc:
        parts = []
        parse_issue = str(exc)

    python3_present = bool(parts and parts[0] == 'python3')
    script_path_present = str(expected_script) in parts
    har_flag_present = '--har-path' in parts
    har_path_present = str(expected_har) in parts
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
    if not har_flag_present:
        issues.append('review command missing --har-path')
    if not har_path_present:
        issues.append(f'review command missing HAR path: {expected_har}')
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
        'expected_har_path': str(expected_har),
        'path': str(expected_script),
        'path_exists': script_exists,
        'script_exists': script_exists,
        'script_is_file': script_is_file,
        'python3_present': python3_present,
        'script_path_present': script_path_present,
        'har_flag_present': har_flag_present,
        'har_path_present': har_path_present,
        'json_flag_present': json_flag_present,
        'parse_issue': parse_issue,
        'status': 'valid' if not issues else 'invalid',
        'valid': not issues,
        'requires_executable': False,
        'issue': issues[0] if issues else None,
        'issues': issues,
    }


def issue_record(message: str, har_path: Path, script_path: Path | None = None) -> dict[str, Any]:
    script = SCRIPT_PATH if script_path is None else Path(script_path)
    command = diagnostic_command(script, har_path)
    validation = review_command_validation(command, script, har_path)
    return {
        'title': 'Baselane batch split review',
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


def load_har_auth(har_path: Path | str = HAR_PATH) -> list[dict[str, Any]]:
    """Extract cookies from HAR for authentication."""
    path = Path(har_path)

    if path.suffix == '.gz' or path.read_bytes()[:2] == b'\x1f\x8b':
        with gzip.open(path, 'rt') as f:
            data = json.load(f)
    else:
        with open(path) as f:
            data = json.load(f)

    cookies = []
    for entry in data.get('log', {}).get('entries', []):
        for cookie in entry.get('request', {}).get('cookies', []):
            cookies.append({
                'name': cookie.get('name'),
                'value': cookie.get('value'),
                'domain': cookie.get('domain'),
                'path': cookie.get('path', '/'),
            })

    # Dedupe by name
    seen = set()
    unique = []
    for c in cookies:
        if c['name'] not in seen:
            seen.add(c['name'])
            unique.append(c)

    return unique


def make_graphql_request(query: str, variables: dict[str, Any], cookies: list[dict[str, Any]]) -> None:
    """Make a GraphQL request using the HAR auth cookies via CDP."""
    payload = json.dumps({'query': query, 'variables': variables})

    # Write payload to temp file
    with open('/tmp/split_gql_payload.json', 'w') as f:
        f.write(payload)

    # Use CDP to make the request via the authenticated browser
    # We need to inject cookies and call the GraphQL endpoint
    script = f'''
const cookies = {json.dumps(cookies)};
const payload = {json.dumps(payload)};

async function main() {{
    const version = await (await fetch('http://127.0.0.1:9222/json/version')).json();
    const ws = new WebSocket(version.webSocketDebuggerUrl);
    let id = 0;
    const pending = new Map();

    ws.onmessage = ev => {{
        const msg = JSON.parse(ev.data);
        if (msg.id && pending.has(msg.id)) {{
            const {{resolve, reject}} = pending.get(msg.id);
            pending.delete(msg.id);
            if (msg.error) reject(msg.error); else resolve(msg.result);
        }}
    }};

    await new Promise((res, rej) => {{ ws.onopen = res; ws.onerror = rej; }});

    // Get Baselane tab
    const targets = await new Promise(res => ws.onmessage = ev => {{
        const msg = JSON.parse(ev.data);
        if (msg.id && pending.has(msg.id)) {{
            const {{resolve}} = pending.get(msg.id);
            pending.delete(msg.id);
            resolve(msg.result);
        }}
    }});

    // Attach to tab and inject cookies
    // Then make the fetch call
    console.log('Making request with payload:', payload);
}}

main().catch(console.error);
'''
    _ = script

    print("NOTE: This function needs CDP execution context")
    return None


def build_split_mutation(parent_txn_id: str, splits: list[dict[str, Any]], split_type: str = 'AMOUNT') -> tuple[str, dict[str, Any]]:
    """Build the createOrUpdateSplitTx mutation for a single transaction."""
    mutation = '''
mutation createOrUpdateSplitTx($parentTransactionId: ID!, $splitType: SplitType!, $transactionSplitInputs: [TransactionSplitInput!]!) {
  createOrUpdateSplitTx(
    input: {parentTransactionId: $parentTransactionId, transactionSplitInputs: $transactionSplitInputs, splitType: $splitType}
  ) {
    id
    splitTransactions {
      id
      tagId
      date
      propertyId
      amount
      merchantName
    }
  }
}'''

    variables = {
        'parentTransactionId': str(parent_txn_id),
        'splitType': split_type,
        'transactionSplitInputs': splits,
    }

    return mutation, variables


def build_report(
    har_path: Path | str = HAR_PATH,
    env: dict[str, str] | None = None,
    script_path: Path | None = None,
) -> dict[str, Any]:
    path = Path(har_path)
    env_map = os.environ if env is None else env
    script = SCRIPT_PATH if script_path is None else Path(script_path)
    records: list[dict[str, Any]] = []

    har_exists = path.exists()
    har_is_file = path.is_file()
    if not har_exists:
        records.append(issue_record('Baselane HAR auth capture is missing', path, script))
    elif not har_is_file:
        records.append(issue_record('Baselane HAR auth capture path is not a file', path, script))

    cdp_session_present = bool(env_map.get('BASELANE_CDP_SESSION'))
    if not cdp_session_present:
        records.append(issue_record('BASELANE_CDP_SESSION is not set for reviewed non-JSON execution', path, script))

    property_id_count = len(PROP_IDS)
    tag_id_count = len({
        TAG_PRINCIPAL,
        TAG_INTEREST,
        TAG_ESCROW,
        TAG_TAXES,
        TAG_INSURANCE,
        TAG_WATER,
    })
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
        'har_path': str(path),
        'har_exists': har_exists,
        'har_is_file': har_is_file,
        'har_read_attempted': False,
        'har_parse_attempted': False,
        'har_cookie_extract_attempted': False,
        'har_cookie_values_included': False,
        'cookie_names_included': False,
        'cookie_domains_included': False,
        'cdp_session_present': cdp_session_present,
        'cdp_session_value_included': False,
        'property_id_count': property_id_count,
        'property_names_included': False,
        'tag_id_count': tag_id_count,
        'tag_values_included': False,
        'bank_account_configured': isinstance(BANK_ACCOUNT_ID, int),
        'bank_account_id_included': False,
        'planned_har_read_count': 1 if har_exists and har_is_file else 0,
        'planned_cookie_extract_count': 1 if har_exists and har_is_file else 0,
        'planned_cdp_session_required_count': 1,
        'planned_graphql_mutation_count': 0,
        'planned_temp_payload_write_count': 0,
        'planned_total_filesystem_mutation_count': 0,
        'file_read_attempted': False,
        'file_write_attempted': False,
        'temp_payload_write_attempted': False,
        'cdp_request_attempted': False,
        'graphql_request_attempted': False,
        'graphql_mutation_attempted': False,
        'subprocess_attempted': False,
        'network_attempted': False,
        'delete_attempted': False,
        'sync_attempted': False,
        'restart_attempted': False,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Inspect Baselane batch split prerequisites')
    parser.add_argument('--har-path', default=HAR_PATH, help='HAR file with Baselane auth cookies')
    parser.add_argument('--json', action='store_true', help='Emit a no-action diagnostic report')
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, stdout=sys.stdout) -> int:
    args = parse_args(argv)
    if args.json:
        print(json.dumps(build_report(args.har_path), indent=2, sort_keys=True), file=stdout)
        return 0

    print("Baselane Batch Split Automation", file=stdout)
    print("=" * 50, file=stdout)

    # Load cookies from HAR
    cookies = load_har_auth(args.har_path)
    print(f"Loaded {len(cookies)} cookies from HAR", file=stdout)

    # Check for required cookies
    cookie_names = [c['name'] for c in cookies]
    print(f"Cookie names: {', '.join(cookie_names[:10])}...", file=stdout)

    print("\nThis script requires a live CDP session.", file=stdout)
    print("Usage: BASELANE_CDP_SESSION=<session_id> python baselane_batch_split.py", file=stdout)
    return 0


if __name__ == '__main__':
    sys.exit(main())
