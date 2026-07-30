#!/usr/bin/env python3
"""
Wyoming Secretary of State Transaction Splitter
Categorizes and splits WY SOS transactions across multiple LLCs.
"""

import argparse
import csv
import json
import re
from pathlib import Path
from collections import defaultdict, Counter
from datetime import datetime
from typing import Any

# Dropbox paths for Wyoming receipts (in Public/03 - LLC Documents)
WYOMING_RECEIPT_PATHS = [
    "/mnt/c/Users/digit/Dropbox/Entities/LFTY0412 LLC",
    "/mnt/c/Users/digit/Dropbox/Entities/LFTY400 LLC",
    "/mnt/c/Users/digit/Dropbox/Real Estate/FL/27 Pillar Ln, Palm Coast, FL 32164/Public/03 - LLC Documents",
    "/mnt/c/Users/digit/Dropbox/Real Estate/FL/49 Bannbury Ln, Palm Coast, FL 32137/Public/03 - LLC Documents",
]

# Danville IL properties (6 properties)
DANVILLE_PROPERTIES = [
    "20 Tennessee Ave",
    "27 S Beard St",
    "1104 E Seminary St",
    "109 Illinois",
    "39 S Virginia Ave",
    "1008 E Madison",
]

# Known Wyoming SOS amount patterns
WYOMING_AMOUNTS = {
    -62.25: "Annual Report Filing",
    -61.44: "Filing Fee",
    -103.75: "Combined Filing",
    -102.40: "Amendment Filing",
}


def scan_wyoming_receipts(receipt_paths: list[str]) -> list[dict[str, Any]]:
    """Scan Dropbox folders for Wyoming SOS receipts."""
    receipts = []

    for path_str in receipt_paths:
        path = Path(path_str)
        if not path.exists():
            print(f"Warning: Receipt path does not exist: {path}")
            continue

        # Look for PDF and image receipts
        for ext in ["*.pdf", "*.PDF", "*.png", "*.jpg", "*.jpeg"]:
            for receipt_file in path.glob(ext):
                # Parse filename for clues
                name = receipt_file.stem.upper()

                # Extract amount from filename if present
                amount_match = re.search(r'(\d+\.\d{2})', name)
                amount = float(amount_match.group(1)) if amount_match else None

                # Determine property from path
                property_name = None
                if "27 PILLAR" in name or "27 Pillar" in str(receipt_file):
                    property_name = "27 Pillar Ln"
                elif "49 BANNBURY" in name or "49 Bannbury" in str(receipt_file):
                    property_name = "49 Bannbury Ln"

                receipts.append({
                    "file": str(receipt_file),
                    "filename": receipt_file.name,
                    "amount": amount,
                    "property": property_name,
                    "parsed": False,
                })

    return receipts


def find_wyoming_transactions(csv_path: str) -> list[dict[str, Any]]:
    """Find all Wyoming Secretary of State transactions in Baselane export."""
    wyoming_txns = []

    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            merchant = row.get('Merchant', '').upper()
            if 'WYOMING' in merchant or 'SECRETARY' in merchant or 'I3B*WY' in merchant:
                wyoming_txns.append(row)

    return wyoming_txns


def match_transactions_to_receipts(
    transactions: list[dict],
    receipts: list[dict]
) -> dict[str, list[dict]]:
    """Match Baselane transactions to receipt evidence."""
    matches = defaultdict(list)

    for txn in transactions:
        amount = float(txn.get('Amount', 0))
        date = txn.get('Date', '')
        merchant = txn.get('Merchant', '')

        # Group by amount pattern
        if amount in WYOMING_AMOUNTS:
            txn_type = WYOMING_AMOUNTS[amount]
        else:
            txn_type = "Unknown"

        # Find matching receipts by amount
        matching_receipts = [
            r for r in receipts
            if r['amount'] == abs(amount) or r['amount'] is None
        ]

        matches[txn_type].append({
            "transaction": txn,
            "receipts": matching_receipts,
            "amount": amount,
            "date": date,
        })

    return dict(matches)


def categorize_wyoming_transaction(txn: dict) -> dict[str, str]:
    """Assign proper Type and Category for Wyoming SOS transactions."""
    merchant = txn.get('Merchant', '').upper()
    amount = float(txn.get('Amount', 0))

    # Determine transaction type from amount pattern
    if amount == -62.25:
        return {
            'Type': 'Operating Expenses',
            'Category': 'Tax Licenses & Registrations',
            'Sub-category': 'WY SOS Annual Report',
            'Description': 'Wyoming Secretary of State Annual Report Filing'
        }
    elif amount == -61.44:
        return {
            'Type': 'Operating Expenses',
            'Category': 'Tax Licenses & Registrations',
            'Sub-category': 'WY SOS Filing Fee',
            'Description': 'Wyoming Secretary of State Filing Fee'
        }
    elif amount == -103.75:
        return {
            'Type': 'Operating Expenses',
            'Category': 'Tax Licenses & Registrations',
            'Sub-category': 'WY SOS Combined Filing',
            'Description': 'Wyoming Secretary of State Combined Filing'
        }
    elif amount == -102.40:
        return {
            'Type': 'Operating Expenses',
            'Category': 'Tax Licenses & Registrations',
            'Sub-category': 'WY SOS Amendment',
            'Description': 'Wyoming Secretary of State Amendment Filing'
        }
    else:
        return {
            'Type': 'Operating Expenses',
            'Category': 'Tax Licenses & Registrations',
            'Sub-category': 'WY SOS Other',
            'Description': 'Wyoming Secretary of State Filing'
        }


def generate_split_plan(
    matched_transactions: dict,
    target_properties: list[str]
) -> list[dict[str, Any]]:
    """Generate split transaction plan with categorization."""
    split_plan = []

    for txn_type, txns in matched_transactions.items():
        for txn_data in txns:
            txn = txn_data['transaction']
            amount = float(txn['Amount'])

            # Get categorization
            categorization = categorize_wyoming_transaction(txn)

            # Determine split method based on transaction type
            if txn_type == "Annual Report Filing" and abs(amount) > 500:
                # Large amount likely covers multiple LLCs - split equally
                property_count = len(target_properties)
                split_amount = round(amount / property_count, 2)

                for prop in target_properties:
                    split_entry = {
                        "original_txn": txn,
                        "split_property": prop,
                        "split_amount": split_amount,
                        "split_type": "equal",
                        "reason": f"{txn_type} split equally among {property_count} LLCs",
                        **categorization
                    }
                    split_plan.append(split_entry)
            else:
                # Standard filing - assign to specific LLC based on Property field
                assigned_property = txn.get('Property', 'Unknown')
                split_entry = {
                    "original_txn": txn,
                    "split_property": assigned_property,
                    "split_amount": amount,
                    "split_type": "single",
                    "reason": f"{txn_type} assigned to specific LLC",
                    **categorization
                }
                split_plan.append(split_entry)

    return split_plan


def main():
    parser = argparse.ArgumentParser(description='Wyoming SOS Transaction Splitter')
    parser.add_argument('--source', required=True, help='Baselane CSV source file')
    parser.add_argument('--output', required=True, help='Output split plan JSON')
    parser.add_argument('--dry-run', action='store_true', help='Preview only')
    parser.add_argument('--properties', nargs='+', default=DANVILLE_PROPERTIES,
                       help='Properties to split across')
    args = parser.parse_args()

    print(f"Scanning Wyoming receipts...")
    receipts = scan_wyoming_receipts(WYOMING_RECEIPT_PATHS)
    print(f"  Found {len(receipts)} receipt files")

    print(f"\\nFinding Wyoming transactions...")
    wyoming_txns = find_wyoming_transactions(args.source)
    print(f"  Found {len(wyoming_txns)} Wyoming SOS transactions")

    # Show sample
    if wyoming_txns:
        print(f"\\nSample transactions:")
        for txn in wyoming_txns[:5]:
            print(f"  {txn.get('Date')} | {txn.get('Merchant')[:40]:40s} | {txn.get('Amount'):10s} | {txn.get('Property', 'Unassigned')}")

    print(f"\\nMatching transactions to receipts...")
    matches = match_transactions_to_receipts(wyoming_txns, receipts)

    print(f"\\nGenerating split plan...")
    split_plan = generate_split_plan(matches, args.properties)

    report = {
        "generated_at": datetime.now().isoformat(),
        "receipts_found": len(receipts),
        "wyoming_transactions": len(wyoming_txns),
        "split_plan_count": len(split_plan),
        "transactions_by_type": {k: len(v) for k, v in matches.items()},
        "split_plan": split_plan[:50],  # First 50 for preview
    }

    with open(args.output, 'w') as f:
        json.dump(report, f, indent=2)

    print(f"\\nSplit plan saved to: {args.output}")
    print(f"Total splits to apply: {len(split_plan)}")

    if args.dry_run:
        print("\\n(DRY RUN - no changes applied)")


if __name__ == '__main__':
    main()
