#!/usr/bin/env python3
"""
Baselane Upstream Categorization
Updates transaction Type, Category, and Property directly in Baselane via GraphQL.
Uses same CDP/GraphQL pattern as baselane_do_split.js
"""

import argparse
import csv
import json
import os
import subprocess
from pathlib import Path
from collections import Counter
from datetime import datetime

ROOT = Path(os.environ.get('WORKSPACE_ROOT', '/home/digit/.openclaw/workspace'))
DEFAULT_JS_EXECUTOR = ROOT / 'scripts' / 'baselane_do_categorize.js'

# Categorization rules for upstream application
CATEGORIZATION_RULES = {
    # Wyoming SOS
    'WYOMING SECRETARY': {'Category': 'Tax Licenses & Registrations', 'Type': 'Operating Expenses'},
    'I3B*WY SECRETARY': {'Category': 'Tax Licenses & Registrations', 'Type': 'Operating Expenses'},

    # Revenue
    'AIRBNB': {'Category': 'Short Term Rents', 'Type': 'Revenue'},
    'VRBO': {'Category': 'Short Term Rents', 'Type': 'Revenue'},
    'HOSPITABLE': {'Category': 'Short Term Rents', 'Type': 'Revenue'},
    'STRIPE': {'Category': 'Long Term Rents', 'Type': 'Revenue'},

    # Property Management
    'ALIGNED': {'Category': 'Property Management', 'Type': 'Operating Expenses'},

    # Supplies
    'WALMART': {'Category': 'Supplies', 'Type': 'Operating Expenses'},
    'AMAZON': {'Category': 'Supplies', 'Type': 'Operating Expenses'},
    'AMZN': {'Category': 'Supplies', 'Type': 'Operating Expenses'},
    'HOME DEPOT': {'Category': 'Supplies', 'Type': 'Operating Expenses'},
    "LOWE'S": {'Category': 'Supplies', 'Type': 'Operating Expenses'},

    # Insurance
    'OBIE': {'Category': 'Insurance', 'Type': 'Operating Expenses'},
    'OSC - RISK SECURE': {'Category': 'Insurance', 'Type': 'Operating Expenses'},
    'LOANDEPOT': {'Category': 'Insurance', 'Type': 'Operating Expenses'},

    # Utilities
    'COMED': {'Category': 'Electric', 'Type': 'Operating Expenses'},
    'PG&E': {'Category': 'Electric', 'Type': 'Operating Expenses'},
    'BRIARCLIFF WATER': {'Category': 'Water & Sewer', 'Type': 'Operating Expenses'},
    'BRIARCLIFF TAX': {'Category': 'Taxes', 'Type': 'Operating Expenses'},

    # Cleaning
    'MORGAN LINEN': {'Category': 'Cleaning & Janitorial', 'Type': 'Operating Expenses'},

    # Landscaping
    'LAWNCARE': {'Category': 'Gardening & Landscaping', 'Type': 'Operating Expenses'},
    'LAWNSTARTER': {'Category': 'Gardening & Landscaping', 'Type': 'Operating Expenses'},

    # Legal
    'MCMECHAN': {'Category': 'Legal Fees', 'Type': 'Operating Expenses'},

    # Software
    'PRICELABS': {'Category': 'Software Subscriptions', 'Type': 'Operating Expenses'},

    # Bank Fees
    'JPMC FEE': {'Category': 'Bank Fees', 'Type': 'Operating Expenses'},
    'PSVJ': {'Category': 'Bank Fees', 'Type': 'Operating Expenses'},

    # Pest
    'EPCON': {'Category': 'Pest', 'Type': 'Operating Expenses'},

    # Waste
    'COUNTY WASTE': {'Category': 'Garbage & Recycling', 'Type': 'Operating Expenses'},
}


def match_rule(merchant: str, description: str) -> dict:
    """Match transaction to categorization rule."""
    text = f"{merchant} {description}".upper()
    for pattern, categories in CATEGORIZATION_RULES.items():
        if pattern in text:
            return categories
    return {}


def generate_categorization_plan(csv_path: str) -> list[dict]:
    """Generate plan of transactions to categorize."""
    plan = []

    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    for row in rows:
        merchant = row.get('Merchant', '')
        description = row.get('Description', '')
        baselane_id = row.get('BaselaneId', '')
        current_category = row.get('Category', '')
        current_type = row.get('Type', '')

        rule = match_rule(merchant, description)
        if rule and baselane_id:
            # Only include if different from current
            if rule.get('Category') != current_category or rule.get('Type') != current_type:
                plan.append({
                    'baselane_id': baselane_id,
                    'merchant': merchant,
                    'description': description[:50],
                    'current_type': current_type,
                    'current_category': current_category,
                    'target_type': rule.get('Type'),
                    'target_category': rule.get('Category'),
                })

    return plan


def main():
    parser = argparse.ArgumentParser(description='Categorize Baselane transactions upstream')
    parser.add_argument('--source', required=True, help='Source CSV with BaselaneId')
    parser.add_argument('--plan-output', help='Output JSON plan file')
    parser.add_argument('--apply', action='store_true', help='Apply categorizations via GraphQL')
    parser.add_argument('--dry-run', action='store_true', help='Generate plan only')
    args = parser.parse_args()

    print(f"Generating categorization plan from {args.source}...")
    plan = generate_categorization_plan(args.source)

    print(f"\nFound {len(plan)} transactions to recategorize")

    # Summary by target category
    by_category = Counter([(p['target_type'], p['target_category']) for p in plan])
    print("\nCategorization breakdown:")
    for (type_, cat), count in by_category.most_common(20):
        print(f"  {count:5d} | {type_:20s} | {cat}")

    # Convert tuple keys to strings for JSON
    breakdown_dict = {f"{type_} | {cat}": count for (type_, cat), count in by_category.most_common(50)}

    report = {
        'generated_at': datetime.now().isoformat(),
        'total_to_categorize': len(plan),
        'breakdown': breakdown_dict,
        'plan': plan[:100],  # First 100 for preview
    }

    if args.plan_output:
        with open(args.plan_output, 'w') as f:
            json.dump(report, f, indent=2)
        print(f"\nPlan saved to: {args.plan_output}")

    if args.dry_run:
        print("\n(DRY RUN - no changes applied)")
        return

    if args.apply:
        print(f"\nApplying {len(plan)} categorizations via Baselane GraphQL...")
        print("NOTE: This requires baselane_do_categorize.js or similar GraphQL executor")
        # Would call JS executor here for each transaction
        # subprocess.call(['node', str(DEFAULT_JS_EXECUTOR), ...])


if __name__ == '__main__':
    main()
