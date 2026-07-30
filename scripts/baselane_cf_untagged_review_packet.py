#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).absolute().parents[1]
CF_SCRIPT_DIR = ROOT / "skills" / "baselane-financials" / "scripts"
sys.path.insert(0, str(CF_SCRIPT_DIR))

import update_cf_statements as cf  # noqa: E402
import baselane_ecogl_data_quality_autonomy as autonomy  # noqa: E402


SUGGESTED_BASELANE_CATEGORY = {
    "Rental Income": "Rents",
    "Insurance": "Insurance",
    "Utilities": "Utilities",
    "Operating Expenses": "Other Operating Expenses",
    "Management Fees": "Management Fees",
    "Mortgage Payments (P&I)": "Mortgage Payments",
    "Inter-Account Transfers": "Transfers Between Accounts",
    "Owner Distributions": "Owner Contributions/Distributions",
    "Uncategorized Income": "",
    "Uncategorized Expense": "",
}


CSV_FIELDS = [
    "Date",
    "Amount",
    "Property",
    "Merchant",
    "Description",
    "Notes",
    "Account",
    "suggested_cf_category",
    "suggested_baselane_category",
    "review_required",
    "review_reason",
]


def build_packet(gl_csv: Path, month: str, source_index: Path | None = None) -> dict[str, Any]:
    year, month_num = [int(part) for part in month.split("-", 1)]
    transactions = cf.filter_by_month(cf.load_gl_data(gl_csv), year, month_num)
    source_buckets: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    source_index_metadata: dict[str, Any] = {
        "source_index_status": "not_configured",
        "source_index_path": str(source_index) if source_index else "",
        "source_index_resolved_count": 0,
    }
    if source_index:
        source_buckets, source_index_metadata = autonomy.source_index_rows(source_index.parent.parent)
        source_index_metadata["source_index_path"] = str(source_index)
    rows = []
    for row in transactions:
        if "ecogl_accrual_overlay_id=" in str(row.get("Notes") or ""):
            continue
        if not row.get("_is_fallback"):
            continue
        suggested_cf_category = row.get("_cf_category") or ""
        suggested_baselane_category = SUGGESTED_BASELANE_CATEGORY.get(suggested_cf_category, "")
        review_required = not suggested_baselane_category or suggested_baselane_category == "Other Operating Expenses"
        source_matches = autonomy.source_index_matches(row, source_buckets) if source_index else []
        rows.append({
                "Date": row.get("Date", ""),
                "Amount": row.get("Amount", ""),
                "Property": row.get("_property") or row.get("Property", ""),
                "Merchant": row.get("Merchant", ""),
                "Description": row.get("Description", ""),
                "Notes": row.get("Notes", ""),
                "Account": row.get("Account", ""),
                "suggested_cf_category": suggested_cf_category,
                "suggested_baselane_category": suggested_baselane_category,
                "review_required": review_required,
                "review_reason": "needs_specific_category" if review_required else "category_suggested_from_deterministic_fallback",
                "source_index_resolved": bool(source_matches),
                "source_index_match_count": len(source_matches),
                "source_index_tag_ids": sorted({str(item.get("TagId")) for item in source_matches if str(item.get("TagId") or "").strip()}),
                "source_index_category": source_matches[0].get("Category", "") if source_matches else "",
                "source_index_type": source_matches[0].get("Type", "") if source_matches else "",
            })
    merchant_counts = Counter(row["Merchant"] for row in rows)
    category_counts = Counter(row["suggested_cf_category"] for row in rows)
    property_counts = Counter(row["Property"] for row in rows)
    unresolved_rows = [row for row in rows if not row["source_index_resolved"]]
    source_index_metadata["source_index_resolved_count"] = len(rows) - len(unresolved_rows)
    return {
        "status": "review" if unresolved_rows else "ok",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "month": month,
        "gl_csv": str(gl_csv),
        "untagged_row_count": len(rows),
        "review_required_count": sum(1 for row in rows if row["review_required"]),
        "auto_suggested_count": sum(1 for row in rows if not row["review_required"]),
        "raw_untagged_row_count": len(rows),
        "raw_review_required_count": sum(1 for row in rows if row["review_required"]),
        "effective_untagged_row_count": len(unresolved_rows),
        "effective_review_required_count": sum(1 for row in unresolved_rows if row["review_required"]),
        **source_index_metadata,
        "merchant_counts": dict(merchant_counts.most_common()),
        "suggested_cf_category_counts": dict(category_counts.most_common()),
        "property_counts": dict(property_counts.most_common()),
        "rows": rows,
    }


def write_outputs(packet: dict[str, Any], json_path: Path, csv_path: Path, markdown_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(packet, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in packet["rows"]:
            writer.writerow({field: row.get(field, "") for field in CSV_FIELDS})
    lines = [
        f"# Baselane CF Untagged Review — {packet['month']}",
        "",
        f"- Status: {packet['status']}",
        f"- Raw fallback rows: {packet.get('raw_untagged_row_count', packet['untagged_row_count'])}",
        f"- Effective unresolved rows: {packet.get('effective_untagged_row_count', packet['untagged_row_count'])}",
        f"- Auto-suggested tags: {packet['auto_suggested_count']}",
        f"- Raw rows needing specific review: {packet['review_required_count']}",
        f"- Effective rows needing review: {packet.get('effective_review_required_count', packet['review_required_count'])}",
        f"- CSV: `{csv_path}`",
        "",
        "## Top Merchants",
    ]
    for merchant, count in list(packet["merchant_counts"].items())[:25]:
        lines.append(f"- {merchant or '(blank)'}: {count}")
    lines.extend(["", "## Suggested CF Categories"])
    for category, count in packet["suggested_cf_category_counts"].items():
        lines.append(f"- {category or '(blank)'}: {count}")
    lines.append("")
    markdown_path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a review packet for Baselane GL rows still using CF keyword fallback.")
    parser.add_argument("--gl-csv", required=True, type=Path)
    parser.add_argument("--source-index", type=Path, help="Current daily source transaction index used to resolve stale exported GL categories.")
    parser.add_argument("--month", required=True)
    parser.add_argument("--json", required=True, type=Path)
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--markdown", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    packet = build_packet(args.gl_csv, args.month, args.source_index)
    write_outputs(packet, args.json, args.csv, args.markdown)
    print(json.dumps({k: packet[k] for k in ["status", "untagged_row_count", "effective_untagged_row_count", "auto_suggested_count", "review_required_count", "effective_review_required_count"]}, indent=2))
    return 0 if packet["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
