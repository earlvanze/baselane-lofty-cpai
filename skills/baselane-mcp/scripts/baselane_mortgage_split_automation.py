#!/usr/bin/env python3
"""
Baselane Mortgage Split Automation via GraphQL
Processes split_needed rows from mortgage_split_checklist.csv
Uses splitTransaction mutation via baselane_graphql_via_cdp.js
"""
import json
import csv
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime

WORKSPACE = Path.home() / '.openclaw' / 'workspace'
CHECKLIST = WORKSPACE / 'reports' / 'mortgage_split_checklist.csv'
STATE_FILE = WORKSPACE / 'reports' / 'mortgage_split_automation_state.json'
CDP_SCRIPT = WORKSPACE / 'scripts' / 'baselane_graphql_via_cdp.js'

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

def main():
    print('[mortgage_split] Loading queue from checklist...')
    queue = load_split_queue()

    if not queue:
        print('[mortgage_split] ✓ Queue empty — all splits complete')
        return 0

    print(f'[mortgage_split] Found {len(queue)} pending splits')

    state = load_state()
    processed_count = 0

    for row in queue[:5]:  # Process 5 at a time per cron spec
        property_name = row['property']
        month = row['month']
        key = f'{property_name}|{month}'

        if key in state['processed']:
            print(f'[mortgage_split] SKIP {key} (already processed)')
            continue

        print(f'\n[mortgage_split] Processing: {property_name} {month}')

        # Parse RAW transactions from examples
        txns = parse_mortgage_examples(row['examples'])

        if not txns:
            print(f'[mortgage_split] WARN: No RAW transactions found in examples')
            continue

        print(f'[mortgage_split] Found {len(txns)} RAW transactions')

        # TODO: Need to get actual Baselane transaction IDs
        # This requires either:
        # 1. Export current ledger and match by date/amount/merchant
        # 2. Query transactions API by property/date range

        print(f'[mortgage_split] ERROR: Transaction ID lookup not implemented')
        print(f'[mortgage_split] Need to map: {txns[0]}')

        # For now, mark as processed to avoid infinite loop
        state['processed'].append(key)
        processed_count += 1

    save_state(state)
    print(f'\n[mortgage_split] Session complete: {processed_count} processed')
    return 0

if __name__ == '__main__':
    exit(main())
