#!/usr/bin/env python3
"""
Find Baselane transactions missing property tags
"""

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path('/home/digit/.openclaw/workspace')
CSV_PATH = ROOT / 'reports' / 'baselane_source_transaction_index.csv'

def main():
    transactions = []
    with open(CSV_PATH, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            transactions.append(row)

    # Find transactions with empty Property
    missing_property = []
    for txn in transactions:
        prop = txn.get('Property', '').strip()
        if not prop:
            missing_property.append(txn)

    print("=" * 80)
    print(f"TRANSACTIONS MISSING PROPERTY TAGS: {len(missing_property)}")
    print("=" * 80)

    # Group by merchant
    by_merchant = Counter()
    by_category = Counter()
    by_type_category = Counter()

    # Detailed breakdown
    details = []

    for txn in missing_property:
        merchant = txn.get('Merchant', 'Unknown')[:60]
        category = txn.get('Category', 'Unknown')
        txn_type = txn.get('Type', 'Unknown')
        amount = txn.get('Amount', '0')

        by_merchant[merchant] += 1
        by_category[category] += 1
        by_type_category[f"{txn_type} | {category}"] += 1

        details.append({
            'id': txn.get('BaselaneId'),
            'date': txn.get('Date'),
            'merchant': merchant,
            'amount': amount,
            'type': txn_type,
            'category': category,
            'sub_category': txn.get('Sub-category', ''),
            'description': txn.get('Description', '')[:80]
        })

    print("\n--- BY TYPE | CATEGORY ---")
    for tc, count in by_type_category.most_common(30):
        print(f"  {count:4d} | {tc}")

    print("\n--- TOP MERCHANTS ---")
    for merch, count in by_merchant.most_common(30):
        print(f"  {count:4d} | {merch}")

    # Look for specific patterns
    wyoming_count = sum(1 for t in missing_property if 'WY SECRETARY' in t.get('Merchant', '').upper() or 'ILLINOIS SECRETARY' in t.get('Merchant', '').upper())
    osc_count = sum(1 for t in missing_property if 'OSC' in t.get('Merchant', '').upper() or 'OBIE' in t.get('Merchant', '').upper())
    walmart_count = sum(1 for t in missing_property if 'WALMART' in t.get('Merchant', '').upper())
    amazon_count = sum(1 for t in missing_property if 'AMAZON' in t.get('Merchant', '').upper())

    print(f"\n--- SPECIFIC VENDORS ---")
    print(f"  Wyoming/Secretary of State: {wyoming_count}")
    print(f"  OSC Risk Secure/Obie: {osc_count}")
    print(f"  Walmart: {walmart_count}")
    print(f"  Amazon: {amazon_count}")

    print("\n--- SAMPLE TRANSACTIONS (first 20) ---")
    for d in details[:20]:
        print(f"\n  ID: {d['id']}")
        print(f"  Date: {d['date']} | Amount: {d['amount']}")
        print(f"  Merchant: {d['merchant']}")
        print(f"  Type: {d['type']} | Category: {d['category']}")
        print(f"  Desc: {d['description']}")

    # Save full report
    report = {
        'total_missing': len(missing_property),
        'by_merchant': dict(by_merchant.most_common(50)),
        'by_category': dict(by_category.most_common(30)),
        'by_type_category': dict(by_type_category.most_common(30)),
        'transactions': details
    }

    report_path = ROOT / 'reports' / 'baselane_missing_properties.json'
    with open(report_path, 'w') as f:
        json.dump(report, f, indent=2)

    print(f"\n--- REPORT SAVED ---")
    print(f"  {report_path}")

if __name__ == '__main__':
    main()
