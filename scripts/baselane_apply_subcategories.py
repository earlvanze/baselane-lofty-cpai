#!/usr/bin/env python3
"""
Baselane Sub-Category Automation
Applies sub-category rules to transactions based on merchant/description patterns.
"""

import csv
import json
import re
import argparse
from pathlib import Path
from collections import Counter
from datetime import datetime

DEFAULT_RULES_PATH = Path('/home/digit/.openclaw/workspace/config/baselane_automation_rules.json')
DEFAULT_SOURCE_PATH = Path('/home/digit/.openclaw/workspace/reports/baselane_source_transaction_index.csv')
DEFAULT_OUTPUT_PATH = Path('/home/digit/.openclaw/workspace/reports/baselane_subcategory_applied.csv')

# Sub-category patterns mapped from automation rules
SUBCATEGORY_PATTERNS = {
    'Supplies': [
        (r'WALMART|WALMART\.COM', 'Supplies - Walmart'),
        (r'HOME DEPOT', 'Supplies - Home Depot'),
        (r'LOWE', "Supplies - Lowe's"),
        (r'AMAZON|AMZN', 'Supplies - Amazon'),
        (r'SAM.*CLUB', "Supplies - Sam's Club"),
    ],
    'Short Term Rents': [
        (r'AIRBNB', 'Airbnb Revenue'),
        (r'VRBO', 'VRBO Revenue'),
        (r'HOSPITABLE', 'Hospitable Revenue'),
        (r'EVOLVE', 'Evolve Revenue'),
    ],
    'Long Term Rents': [
        (r'STRIPE', 'Stripe Payments'),
        (r'ALIGNED', 'Aligned Properties'),
        (r'BASELANE', 'Baselane Direct'),
    ],
    'Insurance': [
        (r'LOANDEPOT|LOAN DEPOT', 'LoanDepot Insurance'),
        (r'MORTGAGE.*ESCROW.*INSURANCE', 'Escrow Insurance'),
        (r'OBIE|OSC.*RISK|RISK.*SECURE', 'OSC Risk Secure'),
        (r'FLOOD', 'Flood Insurance'),
    ],
    'Taxes': [
        (r'MORTGAGE.*ESCROW.*TAX', 'Escrow Property Taxes'),
        (r'PROPERTY.*TAX', 'Property Taxes'),
        (r'CITY.*STATE.*LOCAL', 'City/State Taxes'),
        (r'WYOMING.*SECRETARY|SECRETARY.*STATE', 'WY Secretary of State'),
        (r'ILLINOIS.*SECRETARY|CO.*SECRETARY', 'State Secretary Filing'),
    ],
    'Transfers Between Accounts': [
        (r'INTERNAL.*TRANSFER', 'Internal Transfer'),
        (r'TRANSFER.*BETWEEN', 'Internal Transfer'),
    ],
    'Owner Contributions/Distributions': [
        (r'OWNER.*CONTRIBUTION', 'Owner Contribution'),
        (r'OWNER.*DISTRIBUTION', 'Owner Distribution'),
        (r'CONTRIBUTION.*DISTRIBUTION', 'Owner Contribution/Distribution'),
    ],
    'Loan Payments & Capex': [
        (r'PRINCIPAL.*PAYMENT|MORTGAGE.*PRINCIPAL', 'Mortgage Principal'),
        (r'INTEREST.*PAYMENT|MORTGAGE.*INTEREST', 'Mortgage Interest'),
        (r'ESCROW', 'Escrow'),
        (r'CAPEX|FURNITURE|EQUIPMENT', 'Capital Expenditures'),
    ],
    'Cleaning & Janitorial': [
        (r'MORGAN.*LINEN', 'Morgan Linen Services'),
        (r'HEML', 'Hemlane Cleaning'),
    ],
    'Gardening & Landscaping': [
        (r'LAWN', 'Lawn Service'),
        (r'LANDSCAPING', 'Landscaping'),
        (r'LAWNCARE', 'LawnCare'),
    ],
    'Legal Fees': [
        (r'MCMECHAN|LAWYER|ATTORNEY', 'Legal Services'),
    ],
    'Software Subscriptions': [
        (r'PRICELABS', 'PriceLabs'),
        (r'HEMLANE', 'Hemlane Subscription'),
        (r'HOSPITABLE', 'Hospitable Subscription'),
    ],
    'Utilities': [
        (r'COMED', 'ComEd Electric'),
        (r'PG.*E', 'PG&E'),
        (r'BRIARCLIFF.*WATER', 'Briarcliff Water'),
        (r'BRIARCLIFF.*TAX', 'Briarcliff Taxes'),
        (r'PUBLIC.*UTILITIES', 'Public Utilities'),
    ],
    'Bank Fees': [
        (r'JPMC.*FEE|PSVJ.*FEE', 'JPMorgan Fees'),
    ],
    'Pest': [
        (r'EPCON', 'Epcon Lane Pest'),
    ],
    'Garbage & Recycling': [
        (r'COUNTY.*WASTE|WASTE.*MANAGEMENT', 'Waste Services'),
    ],
}


def apply_subcategory_rules(row: dict) -> tuple[str, str]:
    """
    Apply sub-category rules to a transaction row.
    Returns (original_category, sub_category)
    """
    category = row.get('Category', '').strip()
    merchant = row.get('Merchant', '').strip()
    description = row.get('Description', '').strip()
    text = f"{merchant} {description}".upper()

    # Check if category has sub-category rules
    if category in SUBCATEGORY_PATTERNS:
        for pattern, subcat in SUBCATEGORY_PATTERNS[category]:
            if re.search(pattern, text):
                return category, subcat

    return category, ''


def main():
    parser = argparse.ArgumentParser(description='Apply sub-category automation rules to Baselane transactions')
    parser.add_argument('--source', type=Path, default=DEFAULT_SOURCE_PATH, help='Source CSV file')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT_PATH, help='Output CSV file')
    parser.add_argument('--dry-run', action='store_true', help='Preview changes without writing')
    parser.add_argument('--rules', type=Path, default=DEFAULT_RULES_PATH, help='Rules JSON file')
    args = parser.parse_args()

    # Load source data
    with open(args.source, 'r') as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    print(f"Loaded {len(rows)} transactions from {args.source}")

    # Apply rules
    applied_count = 0
    category_subcat_counts = Counter()

    for row in rows:
        original_cat, subcat = apply_subcategory_rules(row)
        if subcat:
            row['Sub-category'] = subcat
            applied_count += 1
            category_subcat_counts[(original_cat, subcat)] += 1

    # Report results
    print(f"\nApplied sub-categories to {applied_count}/{len(rows)} transactions ({100*applied_count/len(rows):.1f}%)")
    print(f"\nTop sub-categories applied:")
    for (cat, subcat), count in category_subcat_counts.most_common(20):
        print(f"  {count:5d} | {cat} → {subcat}")

    # Generate report
    report = {
        'generated_at': datetime.now().isoformat(),
        'source_file': str(args.source),
        'total_transactions': len(rows),
        'subcategories_applied': applied_count,
        'coverage_pct': round(100 * applied_count / len(rows), 1),
        'subcategory_breakdown': dict(category_subcat_counts.most_common(50)),
    }

    report_path = args.output.parent / 'baselane_subcategory_report.json'
    with open(report_path, 'w') as f:
        json.dump(report, f, indent=2)
    print(f"\nReport saved to: {report_path}")

    if not args.dry_run:
        # Write output CSV
        with open(args.output, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        print(f"Output saved to: {args.output}")
    else:
        print("\n(DRY RUN - no output written)")


if __name__ == '__main__':
    main()
