#!/usr/bin/env python3
"""
Baselane Vendor Categorization Automation
Categorizes transactions by vendor patterns for 100% sub-category coverage.
"""

import argparse
import csv
import json
import re
from pathlib import Path
from collections import Counter
from datetime import datetime

# Vendor categorization rules
VENDOR_CATEGORIES = {
    # Revenue
    'AIRBNB': {'Type': 'Revenue', 'Category': 'Short Term Rents', 'Sub-category': 'Airbnb'},
    'VRBO': {'Type': 'Revenue', 'Category': 'Short Term Rents', 'Sub-category': 'VRBO'},
    'HOSPITABLE': {'Type': 'Revenue', 'Category': 'Short Term Rents', 'Sub-category': 'Hospitable'},
    'EVOLVE': {'Type': 'Revenue', 'Category': 'Short Term Rents', 'Sub-category': 'Evolve'},
    'STRIPE': {'Type': 'Revenue', 'Category': 'Long Term Rents', 'Sub-category': 'Stripe Payments'},

    # Property Management
    'ALIGNED': {'Type': 'Operating Expenses', 'Category': 'Property Management', 'Sub-category': 'Aligned Properties'},
    'HEMLANE': {'Type': 'Operating Expenses', 'Category': 'Property Management', 'Sub-category': 'Hemlane'},

    # Supplies & Retail
    'WALMART': {'Type': 'Operating Expenses', 'Category': 'Supplies', 'Sub-category': 'Supplies - Walmart'},
    'AMAZON': {'Type': 'Operating Expenses', 'Category': 'Supplies', 'Sub-category': 'Supplies - Amazon'},
    'AMZN': {'Type': 'Operating Expenses', 'Category': 'Supplies', 'Sub-category': 'Supplies - Amazon'},
    'HOME DEPOT': {'Type': 'Operating Expenses', 'Category': 'Supplies', 'Sub-category': 'Supplies - Home Depot'},
    "LOWE'S": {'Type': 'Operating Expenses', 'Category': 'Supplies', 'Sub-category': "Supplies - Lowe's"},
    "SAM'S CLUB": {'Type': 'Operating Expenses', 'Category': 'Supplies', 'Sub-category': "Supplies - Sam's Club"},

    # Insurance
    'OBIE': {'Type': 'Operating Expenses', 'Category': 'Insurance', 'Sub-category': 'OSC Risk Secure'},
    'OSC - RISK SECURE': {'Type': 'Operating Expenses', 'Category': 'Insurance', 'Sub-category': 'OSC Risk Secure'},
    'LOANDEPOT': {'Type': 'Operating Expenses', 'Category': 'Insurance', 'Sub-category': 'LoanDepot Insurance'},

    # Utilities
    'COMED': {'Type': 'Operating Expenses', 'Category': 'Electric', 'Sub-category': 'ComEd'},
    'PG&E': {'Type': 'Operating Expenses', 'Category': 'Electric', 'Sub-category': 'PG&E'},
    'PGE': {'Type': 'Operating Expenses', 'Category': 'Electric', 'Sub-category': 'PG&E'},
    'BRIARCLIFF WATER': {'Type': 'Operating Expenses', 'Category': 'Water & Sewer', 'Sub-category': 'Briarcliff Water'},
    'BRIARCLIFF TAX': {'Type': 'Operating Expenses', 'Category': 'Taxes', 'Sub-category': 'Briarcliff Taxes'},
    'PUBLIC UTILITIES': {'Type': 'Operating Expenses', 'Category': 'Utilities', 'Sub-category': 'Public Utilities'},

    # Cleaning & Maintenance
    'MORGAN LINEN': {'Type': 'Operating Expenses', 'Category': 'Cleaning & Janitorial', 'Sub-category': 'Morgan Linen'},

    # Landscaping
    'LAWNCARE': {'Type': 'Operating Expenses', 'Category': 'Gardening & Landscaping', 'Sub-category': 'LawnStarter'},
    'LAWN': {'Type': 'Operating Expenses', 'Category': 'Gardening & Landscaping', 'Sub-category': 'Lawn Service'},

    # Legal & Professional
    'MCMECHAN': {'Type': 'Operating Expenses', 'Category': 'Legal Fees', 'Sub-category': 'McMechan Law'},

    # Software
    'PRICELABS': {'Type': 'Operating Expenses', 'Category': 'Software Subscriptions', 'Sub-category': 'PriceLabs'},

    # Bank Fees
    'JPMC FEE': {'Type': 'Operating Expenses', 'Category': 'Bank Fees', 'Sub-category': 'JPMorgan Fees'},
    'PSVJ': {'Type': 'Operating Expenses', 'Category': 'Bank Fees', 'Sub-category': 'JPMorgan Fees'},

    # Pest Control
    'EPCON': {'Type': 'Operating Expenses', 'Category': 'Pest', 'Sub-category': 'Epcon Lane'},

    # Waste
    'COUNTY WASTE': {'Type': 'Operating Expenses', 'Category': 'Garbage & Recycling', 'Sub-category': 'County Waste'},

    # Internal Transfers
    'INTERNAL_TRANSFER': {'Type': 'Transfers & Other', 'Category': 'Transfers Between Accounts', 'Sub-category': 'Internal Transfer'},
}


def categorize_transaction(row: dict) -> dict:
    """Apply vendor categorization rules to a transaction."""
    merchant = row.get('Merchant', '').upper()
    description = row.get('Description', '').upper()
    text = f"{merchant} {description}"

    for pattern, categories in VENDOR_CATEGORIES.items():
        if pattern in text:
            return categories

    return {}


def main():
    parser = argparse.ArgumentParser(description='Categorize Baselane transactions by vendor')
    parser.add_argument('--source', required=True, help='Source CSV file')
    parser.add_argument('--output', required=True, help='Output CSV file')
    parser.add_argument('--report', help='Report JSON file')
    parser.add_argument('--dry-run', action='store_true', help='Preview only')
    args = parser.parse_args()

    with open(args.source, 'r') as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    print(f"Loaded {len(rows)} transactions")

    applied = 0
    category_counts = Counter()

    for row in rows:
        categories = categorize_transaction(row)
        if categories:
            row['Type'] = categories.get('Type', row.get('Type', ''))
            row['Category'] = categories.get('Category', row.get('Category', ''))
            row['Sub-category'] = categories.get('Sub-category', row.get('Sub-category', ''))
            applied += 1
            category_counts[(categories['Type'], categories['Category'], categories['Sub-category'])] += 1

    print(f"\nCategorized {applied}/{len(rows)} transactions ({100*applied/len(rows):.1f}%)")
    print(f"\nTop categories:")
    for (type_, cat, subcat), count in category_counts.most_common(20):
        print(f"  {count:5d} | {type_:20s} | {cat:30s} | {subcat}")

    report = {
        'generated_at': datetime.now().isoformat(),
        'total_transactions': len(rows),
        'categorized': applied,
        'coverage_pct': round(100 * applied / len(rows), 1),
        'category_breakdown': {f"{type_} | {cat} | {subcat}": count for (type_, cat, subcat), count in category_counts.most_common(50)},
    }

    if args.report:
        with open(args.report, 'w') as f:
            json.dump(report, f, indent=2)
        print(f"\nReport saved to: {args.report}")

    if not args.dry_run:
        with open(args.output, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        print(f"Output saved to: {args.output}")
    else:
        print("\n(DRY RUN - no output written)")


if __name__ == '__main__':
    main()
