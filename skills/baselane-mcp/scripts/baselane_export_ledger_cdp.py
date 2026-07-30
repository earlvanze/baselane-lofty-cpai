#!/usr/bin/env python3
import csv
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

import subprocess
import tempfile

REPORTS_DIR = Path('/home/umbrel/.openclaw/workspace/reports')
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
TRACKER_DIR = Path('/home/umbrel/.openclaw/workspace/Dropbox/Projects/transaction_tracker')
OUT_PATH = TRACKER_DIR / 'ECO Systems General Ledger.csv'
GUARD_REPORT_PATH = REPORTS_DIR / 'baselane_export_guard_last.json'
ALERT_PATH = REPORTS_DIR / 'baselane_weekly_alerts.txt'

EXPECTED_SELECTED = int(os.environ.get('BASELANE_EXPECTED_SELECTED', '0'))
MIN_ROWS = int(os.environ.get('BASELANE_MIN_ROWS', '6000'))
MAX_ROWS = int(os.environ.get('BASELANE_MAX_ROWS', '25000'))

EXCLUDE_RAW = {
    '1 Coolwood Dr',
    '3880 Dover St.',
    '3880 Dover St',
    'Crypto Investments',
    'Dome',
    'EVCO Holdings',
    'Mining, Sales, Consulting, and PM',
    'Mining, Sales, Consulting, & PM',
    'NARWALL Holdings',
    'Personal',
    'Vehicles',
}


FIELDS = [
    'Account',
    'Date',
    'Merchant',
    'Description',
    'Amount',
    'Type',
    'Category',
    'Sub-category',
    'Property',
    'Unit',
    'Notes',
]


def normalize_name(v: str) -> str:
    s = (v or '').strip().lower().replace('&', ' and ')
    s = re.sub(r'[^a-z0-9]+', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


EXCLUDE_NORM = {normalize_name(x) for x in EXCLUDE_RAW}
EXCLUDE_TOKEN_RULES = {
    normalize_name('1 Coolwood Dr'): ['1', 'coolwood'],
    normalize_name('3880 Dover St.'): ['3880', 'dover'],
    normalize_name('Crypto Investments'): ['crypto', 'investments'],
    normalize_name('Dome'): ['dome'],
    normalize_name('EVCO Holdings'): ['evco', 'holdings'],
    normalize_name('Mining, Sales, Consulting, and PM'): ['mining', 'sales', 'consulting', 'pm'],
    normalize_name('NARWALL Holdings'): ['narwall', 'holdings'],
    normalize_name('Personal'): ['personal'],
    normalize_name('Vehicles'): ['vehicles'],
}


def append_alert(line: str):
    with ALERT_PATH.open('a', encoding='utf-8') as f:
        f.write(f"[{datetime.now().isoformat()}] {line}\n")


def write_csv(path: Path, rows):
    with path.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def main():
    gql_bridge = Path('/home/umbrel/.openclaw/workspace/scripts/baselane_graphql_via_cdp.js')

    def gql(operation_name: str, query: str, variables=None):
        payload = {'operationName': operation_name, 'variables': variables or {}, 'query': query}
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as tf:
            json.dump(payload, tf)
            temp_path = tf.name
        try:
            resp = subprocess.run(
                ['node', str(gql_bridge), temp_path],
                text=True,
                capture_output=True,
                timeout=180,
            )
        finally:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
        if resp.returncode != 0:
            raise RuntimeError(f'GraphQL bridge failed for {operation_name}: {(resp.stderr or resp.stdout)[-800:]}')
        body = json.loads(resp.stdout)
        if body.get('errors'):
            raise RuntimeError(f"GraphQL {operation_name} errors: {body['errors']}")
        return body.get('data', {})

    props = gql('PropertyList', 'query PropertyList { property { id name address } }').get('property', [])
    prop_map = {str(p['id']): p.get('name', '') for p in props}

    excluded_ids = set()
    for p in props:
        if normalize_name(p.get('name') or '') in EXCLUDE_NORM:
            excluded_ids.add(str(p['id']))

    selected_count = len(props) - len(excluded_ids)
    autocorrect_applied = False
    autocorrect_added = []
    selected_property_ids = {str(p['id']) for p in props if str(p['id']) not in excluded_ids}


    tags = gql('TagList', 'query TagList { tag { type subType { id name } } }').get('tag', [])
    tag_map = {}
    for t in tags:
        for st in t.get('subType', []):
            tag_map[str(st['id'])] = (t.get('type', ''), st.get('name', ''))

    bank_cache = {}

    def bank(bid):
        if bid is None:
            return None
        k = str(bid)
        if k in bank_cache:
            return bank_cache[k]
        q = (
            'query BankAccount($id: ID!) '
            '{ bankAccount(id: $id) { id accountName nickName accountNumber institutionName } }'
        )
        v = gql('BankAccount', q, {'id': k}).get('bankAccount')
        bank_cache[k] = v
        return v

    query = '''query Transactions($input: SortsAndFilters) {
      transactions(input: $input) {
        total
        data {
          description
          bankAccountId
          amount
          merchantName
          name
          pending
          time
          hidden
          isDeleted
          isExternal
          isManual
          isSplit
          parentId
          isReviewedByUser
          tagIdSource
          propertyTagIdSource
          tagRuleId
          propertyRuleId
          originalTransaction
          isDocumentUploaded
          linkedAssetId
          linkedLoanId
          id
          tagId
          date
          propertyId
          unitId
          note
        }
      }
    }'''

    all_rows, filtered_rows = [], []
    page_num, limit = 1, 200
    fetched_total = 0

    dropped_excluded_property_rows = 0
    dropped_no_property_rows = 0
    dropped_non_selected_rows = 0
    dropped_unknown_property_rows = 0
    unknown_property_name_rows = 0

    while True:
        variables = {
            'input': {
                'sort': {'field': 'date', 'direction': 'DESC'},
                'filter': {
                    'isHidden': False,
                    'search': '',
                    'isCategorized': None,
                    'tagId': None,
                    'bankAccountId': None,
                    'propertyId': None,
                    'unitId': None,
                    'isDeleted': False,
                    'isDocumentUploaded': None,
                },
                'page': page_num,
                'pageLimit': limit,
            }
        }

        data = gql('Transactions', query, variables).get('transactions', {})
        txs = data.get('data', [])
        if not txs:
            break

        fetched_total += len(txs)

        for tx in txs:
            pid = str(tx.get('propertyId')) if tx.get('propertyId') is not None else None
            prop = prop_map.get(pid, '') if pid else ''

            tag_id = str(tx.get('tagId')) if tx.get('tagId') is not None else None
            ttype, tsub = tag_map.get(tag_id, ('', ''))

            ba = bank(tx.get('bankAccountId'))
            account = ''
            if ba:
                account = f"{ba.get('accountName','')}-{ba.get('nickName','')}-{ba.get('accountNumber','')}"

            date_str = datetime.strptime(tx['date'], '%Y-%m-%d').strftime('%B %d, %Y') if tx.get('date') else ''
            merchant = tx.get('merchantName') or ''
            desc = tx.get('description') or tx.get('name') or merchant
            notes = tx['note'].get('text', '') if isinstance(tx.get('note'), dict) else (str(tx.get('note')) if tx.get('note') else '')

            row = {
                'Account': account,
                'Date': date_str,
                'Merchant': merchant,
                'Description': desc,
                'Amount': tx.get('amount'),
                'Type': ttype,
                'Category': tsub,
                'Sub-category': '',
                'Property': prop or '',
                'Unit': '',
                'Notes': notes,
            }
            all_rows.append(row)

            # Local filtering for canonical export: keep only selected property-scoped rows.
            if pid is None:
                dropped_no_property_rows += 1
                continue
            if pid in excluded_ids or normalize_name(prop) in EXCLUDE_NORM:
                dropped_excluded_property_rows += 1
                continue
            if pid not in selected_property_ids:
                dropped_non_selected_rows += 1
                continue
            if not prop:
                dropped_unknown_property_rows += 1
                prop = f'UNKNOWN_PROPERTY_{pid}'
                unknown_property_name_rows += 1

            out_row = dict(row)
            out_row['Property'] = prop
            filtered_rows.append(out_row)

        page_num += 1

    blank_property_rows = sum(1 for r in filtered_rows if not (r.get('Property') or '').strip())
    excluded_property_rows_in_output = sum(
    1 for r in filtered_rows if normalize_name(r.get('Property') or '') in EXCLUDE_NORM
    )
    unique_props = len({(r.get('Property') or '').strip() for r in filtered_rows if (r.get('Property') or '').strip()})

    now = datetime.now()
    ts = now.strftime('%Y%m%d-%H%M%S')
    tmp_path = OUT_PATH.with_suffix(f'.tmp.{ts}.csv')
    backup_path = OUT_PATH.with_name(f'ECO Systems General Ledger.{ts}.bak.csv')
    snapshot_path = OUT_PATH.with_name(f'ECO Systems General Ledger.filtered.{ts}.csv')
    all_snapshot_path = REPORTS_DIR / f'baselane_export_all_transactions.{ts}.csv'
    filtered_preview_path = REPORTS_DIR / f'baselane_export_filtered_preview.{ts}.csv'

    # Always persist diagnostic snapshots before guard gate for deterministic debugging.
    write_csv(all_snapshot_path, all_rows)
    write_csv(filtered_preview_path, filtered_rows)

    violations = []
    if len(filtered_rows) < MIN_ROWS:
        violations.append(f'row_count_below_min:{len(filtered_rows)}<{MIN_ROWS}')
    if len(filtered_rows) > MAX_ROWS:
        violations.append(f'row_count_above_max:{len(filtered_rows)}>{MAX_ROWS}')
    if blank_property_rows > 0:
        violations.append(f'blank_property_rows:{blank_property_rows}')
    if excluded_property_rows_in_output > 0:
        violations.append(f'excluded_property_rows_in_output:{excluded_property_rows_in_output}')
    if unknown_property_name_rows > 0:
        violations.append(f'unknown_property_name_rows:{unknown_property_name_rows}')

    guard_payload = {
        'ok': len(violations) == 0,
        'timestamp': now.isoformat(),
        'expected_selected': EXPECTED_SELECTED if EXPECTED_SELECTED > 0 else None,
        'actual_selected': selected_count,
        'selected_property_ids_count': len(selected_property_ids),
        'total_properties': len(props),
        'excluded_properties_matched': len(excluded_ids),
        'fetched_total_rows': fetched_total,
        'all_rows_exported_local': len(all_rows),
        'dropped_excluded_property_rows': dropped_excluded_property_rows,
        'dropped_no_property_rows': dropped_no_property_rows,
        'dropped_non_selected_rows': dropped_non_selected_rows,
        'dropped_unknown_property_rows': dropped_unknown_property_rows,
        'output_rows': len(filtered_rows),
        'unique_output_properties': unique_props,
        'blank_property_rows': blank_property_rows,
        'excluded_property_rows_in_output': excluded_property_rows_in_output,
        'unknown_property_name_rows': unknown_property_name_rows,
        'autocorrect_applied': autocorrect_applied,
        'autocorrect_added': autocorrect_added,
        'all_export_snapshot': str(all_snapshot_path),
        'filtered_preview_snapshot': str(filtered_preview_path),
        'filtered_snapshot': str(snapshot_path),
        'canonical_path': str(OUT_PATH),
        'violations': violations,
    }
    GUARD_REPORT_PATH.write_text(json.dumps(guard_payload, indent=2), encoding='utf-8')

    if violations:
        raise RuntimeError('Guard failed: ' + '; '.join(violations))

    shutil.copy2(filtered_preview_path, tmp_path)
    shutil.copy2(tmp_path, snapshot_path)

    if OUT_PATH.exists():
        shutil.copy2(OUT_PATH, backup_path)

    os.replace(tmp_path, OUT_PATH)

    print(f'WROTE {OUT_PATH} rows={len(filtered_rows)} unique_props={unique_props}')
    print(f'ALL_EXPORT_SNAPSHOT {all_snapshot_path} rows={len(all_rows)}')
    print(f'FILTERED_PREVIEW {filtered_preview_path} rows={len(filtered_rows)}')
    print(f'FILTERED_SNAPSHOT {snapshot_path}')
    if backup_path.exists():
        print(f'BACKUP {backup_path}')
    print(f'GUARD_REPORT {GUARD_REPORT_PATH}')

if __name__ == '__main__':
    main()
