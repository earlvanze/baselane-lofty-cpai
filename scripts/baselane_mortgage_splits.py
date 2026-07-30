#!/usr/bin/env python3
"""
Baselane mortgage split executor.
Uses createOrUpdateSplitTx mutation discovered from HAR traffic.
Requires: node, running Baselane tab at CDP port 9222
"""
import argparse
import json, subprocess, sys, time
import os
import shlex
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlparse

CDP_URL = 'http://127.0.0.1:9222'
GRAPHQL_URL = 'https://orchestration.baselane.com/graphql'
ISSUE_CLASS = 'baselane-mortgage-splits'
SCRIPT_PATH = Path(__file__).absolute()


def diagnostic_script_path() -> Path:
    script_name = Path(__file__).name
    raw_path = Path(SCRIPT_PATH)
    excluded_markers = (
        "/mnt/c/Users/digit/Dropbox/Projects/cyber-gateway/config/openclaw/workspace",
        "/mnt/c/users/digit/Dropbox/Projects/cyber-gateway/config/openclaw/workspace",
        "/mnt/f/.openclaw",
    )
    candidate_roots = [
        os.environ.get("WORKSPACE_ROOT"),
        "/home/digit/.openclaw/workspace",
        "/home/umbrel/.openclaw/workspace",
    ]
    for root in candidate_roots:
        if not root:
            continue
        candidate = Path(root) / "scripts" / script_name
        candidate_text = str(candidate)
        if any(marker in candidate_text for marker in excluded_markers):
            continue
        try:
            if candidate.exists() and candidate.samefile(raw_path):
                return candidate
        except OSError:
            continue
    return raw_path


def diagnostic_command() -> str:
    return f'python3 {shlex.quote(str(diagnostic_script_path()))} --json'


DIAGNOSTIC_COMMAND = diagnostic_command()

# Property IDs
PROP_IDS = {
    '90 Madison': 31525,
    '86 Madison': 63162,
    '724 3rd': 33594,
    'Mining PM': 37648,
}

TAG_IDS = {
    'principal': '20',
    'interest': '11',
    'escrow': '130',
    'taxes': '95',
    'insurance': '65',
    'water': '104',
}

def remediation_fields(classification: str):
    has_issues = classification != 'ok'
    return {
        'remediation_class': 'operator-reviewed-baselane-mortgage-splits' if has_issues else 'no-remediation-needed',
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
    expected_script = str(diagnostic_script_path())
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
            'area': 'baselane-mortgage-splits',
            'node_available': evidence.get('node_available'),
            'cdp_url_valid': evidence.get('cdp_url_valid'),
            'graphql_url_valid': evidence.get('graphql_url_valid'),
            'property_id_count': evidence.get('property_id_count'),
            'tag_id_count': evidence.get('tag_id_count'),
            'cdp_eval_attempted': evidence.get('cdp_eval_attempted'),
            'graphql_query_attempted': evidence.get('graphql_query_attempted'),
            'split_mutation_attempted': evidence.get('split_mutation_attempted'),
            'operator_prompt_attempted': evidence.get('operator_prompt_attempted'),
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
    top_level_review_validation = None
    if not classified and report.get('review_command_safe_to_run_automatically'):
        top_level_review_validation = review_command_validation(report.get('review_command'))
        safe_review_command_count = 1
        valid_review_command_count = 1 if top_level_review_validation['valid'] else 0
        validation_issues.extend(top_level_review_validation.get('issues') or [])
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
        'review_command_validation': top_level_review_validation,
        'safe_to_run_automatically': report.get('safe_to_run_automatically') is True,
        'node_available': report.get('node_available') is True,
        'cdp_url_valid': report.get('cdp_url_valid') is True,
        'graphql_url_valid': report.get('graphql_url_valid') is True,
        'property_id_count': int(report.get('property_id_count') or 0),
        'tag_id_count': int(report.get('tag_id_count') or 0),
        'cdp_eval_attempted': report.get('cdp_eval_attempted') is True,
        'token_fetch_attempted': report.get('token_fetch_attempted') is True,
        'graphql_query_attempted': report.get('graphql_query_attempted') is True,
        'split_mutation_attempted': report.get('split_mutation_attempted') is True,
        'operator_prompt_attempted': report.get('operator_prompt_attempted') is True,
        'remediation_class': report.get('remediation_class'),
        'cleanup_command_available_after_review': bool(report.get('cleanup_command_after_review')),
        'restart_command_available_after_review': bool(report.get('restart_command_after_review')),
        'oauth_command_available_after_review': bool(report.get('oauth_command_after_review')),
        'helper_command_available_after_review': bool(report.get('helper_command_after_review')),
    }

def _url_valid(url: str, allowed_schemes):
    parsed = urlparse(url)
    return parsed.scheme in allowed_schemes and bool(parsed.netloc)

def build_report():
    issues = []
    visible_ok = []
    evidence = {
        'script_path': str(SCRIPT_PATH),
        'node_available': shutil.which('node') is not None,
        'cdp_url': CDP_URL,
        'cdp_url_valid': _url_valid(CDP_URL, {'http', 'https'}),
        'graphql_url': GRAPHQL_URL,
        'graphql_url_valid': _url_valid(GRAPHQL_URL, {'https'}),
        'property_id_count': len(PROP_IDS),
        'tag_id_count': len(TAG_IDS),
        'required_tag_keys': sorted(TAG_IDS),
        'required_property_keys': sorted(PROP_IDS),
        'query_page_limit': 50,
        'search_term_configured': 'CITADEL',
        'split_type': 'AMOUNT',
        'cdp_eval_attempted': False,
        'token_fetch_attempted': False,
        'graphql_query_attempted': False,
        'unsplit_query_attempted': False,
        'split_mutation_attempted': False,
        'operator_prompt_attempted': False,
        'node_subprocess_attempted': False,
    }

    if not evidence['node_available']:
        issues.append('Node.js binary is not available for Baselane CDP mortgage split executor')
    if not evidence['cdp_url_valid']:
        issues.append(f'Baselane CDP URL is invalid: {CDP_URL}')
    if not evidence['graphql_url_valid']:
        issues.append(f'Baselane GraphQL URL is invalid: {GRAPHQL_URL}')
    if not PROP_IDS:
        issues.append('Baselane mortgage split property ID map is empty')
    if not TAG_IDS:
        issues.append('Baselane mortgage split tag ID map is empty')
    for required in ('principal', 'interest', 'escrow', 'taxes', 'insurance'):
        if required not in TAG_IDS:
            issues.append(f'Baselane mortgage split required tag mapping is missing: {required}')

    if not issues:
        visible_ok.append(
            'OK Baselane mortgage split executor config: '
            f'node={evidence["node_available"]} properties={evidence["property_id_count"]} tags={evidence["tag_id_count"]}'
        )
        visible_ok.append(
            'OK Baselane mortgage split executor diagnostic: '
            'no CDP eval, token fetch, GraphQL query, split mutation, operator prompt, subprocess, restart, sudo, OAuth, or helper command'
        )

    classification = 'baselane-mortgage-splits-review' if issues else 'ok'
    fields = remediation_fields(classification)
    classified_issues = classified_issue_records(issues, evidence, classification)
    report = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'status': 'BASELANE_MORTGAGE_SPLITS_REVIEW' if issues else 'NO_REPLY',
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
    if summary.get('review_command_validation') is not None:
        report['review_command_valid'] = summary['review_command_validation']['valid']
        report['review_command_validation'] = summary['review_command_validation']
    return report

def cdp_eval(script):
    """Execute JS in Baselane tab via CDP, return result."""
    node_script = f'''
const http = require('http');

async function main() {{
    const version = await (await fetch('{CDP_URL}/json/version')).json();
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

    // Find Baselane tab
    const targets = await new Promise(res => {{
        ws.send(JSON.stringify({{id: ++id, method: 'Target.getTargets'}}));
        ws.onmessage = ev => {{
            const msg = JSON.parse(ev.data);
            if (msg.id) {{ pending.get(msg.id).resolve(msg.result); pending.delete(msg.id); }}
        }};
    }});

    const tab = targets.targetInfos.find(t => t.type === 'page' && t.url.includes('app.baselane.com'));
    if (!tab) {{ console.error('NO_TAB'); process.exit(1); }}

    const attached = await new Promise(res => {{
        ws.send(JSON.stringify({{id: ++id, method: 'Target.attachToTarget', params: {{targetId: tab.targetId, flatten: true}}}}));
        ws.onmessage = ev => {{
            const msg = JSON.parse(ev.data);
            if (msg.id) {{ pending.get(msg.id).resolve(msg.result); pending.delete(msg.id); }}
        }};
    }});
    const sessionId = attached.sessionId;

    // Enable Runtime
    await new Promise(res => {{
        ws.send(JSON.stringify({{id: ++id, method: 'Runtime.enable', sessionId}}));
        ws.onmessage = ev => {{
            const msg = JSON.parse(ev.data);
            if (msg.id) {{ pending.get(msg.id).resolve(msg.result); pending.delete(msg.id); }}
        }};
    }});

    // Evaluate
    const result = await new Promise((res, rej) => {{
        ws.send(JSON.stringify({{id: ++id, method: 'Runtime.evaluate', params: {{expression: `{script}`, awaitPromise: true, returnByValue: true}}, sessionId}}));
        ws.onmessage = ev => {{
            const msg = JSON.parse(ev.data);
            if (msg.id) {{ pending.get(msg.id).resolve(msg.result); pending.delete(msg.id); }}
        }};
    }});

    ws.close();
    console.log(JSON.stringify(result.result ? result.result.value : null));
}}

main().catch(e => {{ console.error('ERROR:', e.message); process.exit(1); }});
'''

    try:
        result = subprocess.run(
            ['node', '-e', node_script],
            capture_output=True, text=True, timeout=30
        )
        if result.stderr:
            for line in result.stderr.strip().split('\n'):
                if not line.startswith('(node:') and 'WARNING' not in line:
                    print(f"  [CDP] {line}", file=sys.stderr)
        output = result.stdout.strip()
        if output.startswith('ERROR:'):
            print(f"  [CDP ERROR] {output}", file=sys.stderr)
            return None
        try:
            return json.loads(output)
        except:
            return output
    except Exception as e:
        print(f"  [CDP EXEC ERROR] {e}", file=sys.stderr)
        return None

def get_appcheck_token():
    """Get fresh appcheck token from live Baselane tab."""
    script = '''
    (async () => {
        // Trigger a request to capture appcheck
        try {
            await fetch('https://orchestration.baselane.com/graphql', {
                method: 'POST',
                credentials: 'include',
                headers: {'content-type': 'application/json'},
                body: JSON.stringify({query: '{ __typename }'})
            });
        } catch(e) {}

        // Find appcheck in cookie or localStorage
        const cookies = document.cookie.split(';');
        for (const c of cookies) {
            if (c.trim().startsWith('firebase-app-check-token')) {
                return 'cookie:' + c.trim();
            }
        }

        // Try to find in a variable
        for (const key of Object.keys(window)) {
            if (key.includes('appcheck') || key.includes('firebase')) {
                try {
                    const v = window[key];
                    if (typeof v === 'object' && v && v.token) return 'var:' + v.token;
                } catch(e) {}
            }
        }

        return 'NO_TOKEN';
    })()
    '''
    result = cdp_eval(script)
    if result and isinstance(result, str) and result.startswith('cookie:'):
        return result.replace('cookie:', '')
    return None

def graphql_query(query, variables):
    """Execute GraphQL query via CDP fetch injection."""
    mutation = query  # For mutations
    script = f'''
    (async () => {{
        const resp = await fetch('https://orchestration.baselane.com/graphql', {{
            method: 'POST',
            credentials: 'include',
            headers: {{
                'content-type': 'application/json',
                'x-firebase-appcheck': '{get_appcheck_token() or "NONE"}'
            }},
            body: JSON.stringify({json.dumps({{'query': mutation, 'variables': variables}})})
        }});
        const text = await resp.text();
        return text;
    }})()
    '''

    result = cdp_eval(script)
    if result:
        try:
            return json.loads(result) if isinstance(result, str) else result
        except:
            return result
    return None

def query_unsplit_citadel():
    """Query for unsplit Citadel mortgage transactions."""
    query = '''
    query GetUnsplits($input: SortsAndFilters) {
      transaction(input: $input) {
        data {
          id
          amount
          date
          merchantName
          propertyId
          tagId
          isSplit
          parentId
        }
      }
    }
    '''
    variables = {
        'input': {
            'sort': {'direction': 'DESC', 'field': 'date'},
            'filter': {
                'search': 'CITADEL',
                'isHidden': False,
                'isDeleted': False,
            },
            'page': 1,
            'pageLimit': 50
        }
    }

    result = graphql_query(query, variables)
    if result and 'data' in result:
        txns = result['data'].get('transaction', {}).get('data', [])
        # Filter for unsplit parent transactions (isSplit=false, no parentId)
        unsplit = [t for t in txns if not t.get('isSplit') and not t.get('parentId')]
        return unsplit
    return []

def execute_split(parent_id, splits):
    """Execute a split for one transaction."""
    mutation = '''
    mutation createOrUpdateSplitTx($parentTransactionId: ID!, $splitType: SplitType!, $transactionSplitInputs: [TransactionSplitInput!]!) {
      createOrUpdateSplitTx(
        input: {parentTransactionId: $parentTransactionId, transactionSplitInputs: $transactionSplitInputs, splitType: $splitType}
      ) {
        id
        splitTransactions {
          id
          tagId
          amount
          merchantName
        }
      }
    }
    '''

    variables = {
        'parentTransactionId': str(parent_id),
        'splitType': 'AMOUNT',
        'transactionSplitInputs': splits
    }

    result = graphql_query(mutation, variables)
    return result

def run(stdout: TextIO | None = None, input_func=input):
    out = stdout or sys.stdout
    print("Baselane Mortgage Split Executor", file=out)
    print("=" * 50, file=out)

    # Step 1: Get appcheck token
    print("\n[1] Getting appcheck token...", file=out)
    token = get_appcheck_token()
    if token:
        print(f"  ✓ Token: {token[:20]}...", file=out)
    else:
        print("  ✗ Could not get token", file=out)
        return 0

    # Step 2: Query unsplit transactions
    print("\n[2] Querying unsplit Citadel transactions...", file=out)
    unsplit = query_unsplit_citadel()
    print(f"  Found {len(unsplit)} unsplit transactions:", file=out)
    for t in unsplit:
        print(f"  {t['id']}  {t['date']}  ${t['amount']}  prop:{t.get('propertyId')}  {t.get('merchantName','')[:40]}", file=out)

    if not unsplit:
        print("  Nothing to split.", file=out)
        return 0

    print("\n[3] Ready to split. Press Enter to proceed, or Ctrl+C to abort.", file=out)
    input_func()
    return 0

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='Run or inspect Baselane mortgage split executor')
    parser.add_argument('--json', action='store_true', help='Emit a read-only diagnostic report and do not query Baselane')
    return parser.parse_args(argv)

def main(argv=None, stdout: TextIO | None = None):
    args = parse_args(argv)
    if args.json:
        report = build_report()
        print(json.dumps(report, indent=2, sort_keys=True), file=stdout or sys.stdout)
        return 0 if report['status'] == 'NO_REPLY' else 1
    return run(stdout=stdout)

if __name__ == '__main__':
    raise SystemExit(main())
