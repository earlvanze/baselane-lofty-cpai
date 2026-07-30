#!/usr/bin/env python3
"""
Next Vendor Categorization: OSC Risk Secure and ECO Systems
Builds categorization rules for insurance and internal transfers.
"""

import csv
import json
from pathlib import Path
from collections import Counter
from datetime import datetime

# Additional vendor categorization rules
ADDITIONAL_CATEGORIES = {
    # OSC Risk Secure (already categorized but ensure consistency)
    'OSC - RISK SECURE': {'Type': 'Operating Expenses', 'Category': 'Insurance', 'Sub-category': 'OSC Risk Secure'},
    'OBIE INSURANCE': {'Type': 'Operating Expenses', 'Category': 'Insurance', 'Sub-category': 'OSC Risk Secure'},

    # ECO Systems internal transfers
    'ECO SYSTEMS': {'Type': 'Transfers & Other', 'Category': 'Transfers Between Accounts', 'Sub-category': 'ECO Systems Internal'},
    'EVCO HOLDINGS': {'Type': 'Transfers & Other', 'Category': 'Owner Contributions/Distributions', 'Sub-category': 'EVCO Holdings'},

    # Additional suppliers
    'MENARDS': {'Type': 'Operating Expenses', 'Category': 'Supplies', 'Sub-category': 'Supplies - Menards'},
    'COSTCO': {'Type': 'Operating Expenses', 'Category': 'Supplies', 'Sub-category': 'Supplies - Costco'},
    'BEST BUY': {'Type': 'Operating Expenses', 'Category': 'Supplies', 'Sub-category': 'Supplies - Best Buy'},
    'TARGET': {'Type': 'Operating Expenses', 'Category': 'Supplies', 'Sub-category': 'Supplies - Target'},

    # Utilities (additional)
    'NICOR': {'Type': 'Operating Expenses', 'Category': 'Gas & Electric', 'Sub-category': 'Nicor Gas'},
    'PEOPLES GAS': {'Type': 'Operating Expenses', 'Category': 'Gas & Electric', 'Sub-category': 'Peoples Gas'},
    'AT&T': {'Type': 'Operating Expenses', 'Category': 'Phone, Cable & Internet', 'Sub-category': 'AT&T'},
    'XFINITY': {'Type': 'Operating Expenses', 'Category': 'Phone, Cable & Internet', 'Sub-category': 'Xfinity'},
    'COMCAST': {'Type': 'Operating Expenses', 'Category': 'Phone, Cable & Internet', 'Sub-category': 'Comcast'},
}


def categorize_additional(row: dict) -> dict:
    """Apply additional vendor categorization."""
    merchant = row.get('Merchant', '').upper()
    description = row.get('Description', '').upper()
    text = f"{merchant} {description}"

    for pattern, categories in ADDITIONAL_CATEGORIES.items():
        if pattern in text:
            return categories
    return {}


def main():
    source_path = Path('/home/digit/.openclaw/workspace/reports/baselane_source_transaction_index.csv')

    with open(source_path, 'r') as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    # Find uncategorized transactions
    uncategorized = []
    for row in rows:
        if not row.get('Sub-category') or row.get('Sub-category', '').strip() == '':
            new_cat = categorize_additional(row)
            if new_cat:
                uncategorized.append({
                    'baselane_id': row.get('BaselaneId'),
                    'merchant': row.get('Merchant'),
                    'description': row.get('Description', '')[:50],
                    'amount': row.get('Amount'),
                    **new_cat
                })
                row['Type'] = new_cat['Type']
                row['Category'] = new_cat['Category']
                row['Sub-category'] = new_cat['Sub-category']

    # Report
    by_cat = Counter([(t['Type'], t['Category'], t['Sub-category']) for t in uncategorized])

    print('ADDITIONAL CATEGORIZATION:')
    print('=' * 70)
    print(f"Newly categorized: {len(uncategorized)}")
    print()
    print('By category:')
    for (type_, cat, subcat), count in by_cat.most_common(20):
        print(f"  {count:4d} | {type_:25s} | {cat:30s} | {subcat}")

    report = {
        'generated_at': datetime.now().isoformat(),
        'newly_categorized': len(uncategorized),
        'by_category': {f"{t} | {c} | {s}": n for (t, c, s), n in by_cat.most_common(50)},
        'transactions': uncategorized[:50]
    }

    report_path = Path('/home/digit/.openclaw/workspace/reports/baselane_additional_categorization.json')
    with open(report_path, 'w') as f:
        json.dump(report, f, indent=2)

    print(f"\nReport saved: {report_path}")


if __name__ == '__main__':
    main()
