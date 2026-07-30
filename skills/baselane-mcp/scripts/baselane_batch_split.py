#!/usr/bin/env python3
"""
Baselane mortgage split automation.
Uses the createOrUpdateSplitTx GraphQL mutation discovered from HAR traffic.
"""
import json, sys, time
from pathlib import Path

# HAR file with auth cookies
HAR_PATH = '/home/umbrel/Downloads/mortgage_splits_app.baselane.com.har'

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

def load_har_auth():
    """Extract cookies from HAR for authentication."""
    import gzip
    har_path = Path(HAR_PATH)

    if har_path.suffix == '.gz' or har_path.read_bytes()[:2] == b'\x1f\x8b':
        import gzip
        with gzip.open(har_path, 'rt') as f:
            data = json.load(f)
    else:
        with open(har_path) as f:
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

def make_graphql_request(query, variables, cookies):
    """Make a GraphQL request using the HAR auth cookies via CDP."""
    import subprocess

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

    print("NOTE: This function needs CDP execution context")
    return None

def build_split_mutation(parent_txn_id, splits, split_type='AMOUNT'):
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
        'transactionSplitInputs': splits
    }

    return mutation, variables

def main():
    print("Baselane Batch Split Automation")
    print("=" * 50)

    # Load cookies from HAR
    cookies = load_har_auth()
    print(f"Loaded {len(cookies)} cookies from HAR")

    # Check for required cookies
    cookie_names = [c['name'] for c in cookies]
    print(f"Cookie names: {', '.join(cookie_names[:10])}...")

    print("\nThis script requires a live CDP session.")
    print("Usage: BASELANE_CDP_SESSION=<session_id> python baselane_batch_split.py")

if __name__ == '__main__':
    main()
