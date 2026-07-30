#!/usr/bin/env python3
"""Generate a high-signal investor financial summary from Baselane ledger + Lofty portfolio.

Outputs:
  - reports/baselane_investor_financial_summary.json  (structured)
  - reports/baselane_investor_financial_summary.md    (human-readable)
  - Appends to UPDATES.md if --append-updates is passed

Data sources:
  1. ECO Systems General Ledger CSV (Baselane export)
  2. Lofty portfolio summary (via loftyassist API or cached JSON)
  3. Split ledger financials report (property-level breakdown)

Usage:
  python3 scripts/baselane_investor_financial_summary.py
  python3 scripts/baselane_investor_financial_summary.py --append-updates
  python3 scripts/baselane_investor_financial_summary.py --json-only
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).parent
WORKSPACE_ROOT = SCRIPT_DIR.parent
REPORT_DIR = WORKSPACE_ROOT / "reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

SUMMARY_JSON = REPORT_DIR / "baselane_investor_financial_summary.json"
SUMMARY_MD = REPORT_DIR / "baselane_investor_financial_summary.md"
UPDATES_MD = WORKSPACE_ROOT / "UPDATES.md"

DEFAULT_LEDGER_PATHS = [
    Path("/data/Dropbox/Projects/assetrail/ECO Systems General Ledger.csv"),
    Path("/mnt/c/Users/digit/Dropbox/Projects/assetrail/ECO Systems General Ledger.csv"),
    Path(os.path.expanduser("~/Dropbox/Projects/assetrail/ECO Systems General Ledger.csv")),
]

LOFTY_PORTFOLIO_CACHE = REPORT_DIR / "lofty_portfolio_cache.json"
SPLIT_FINANCIALS_REPORT = REPORT_DIR / "split_ledger_public_financials_last.json"


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def find_ledger() -> Path | None:
    for p in DEFAULT_LEDGER_PATHS:
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_lofty_portfolio() -> dict[str, Any]:
    """Load Lofty portfolio from cache or return empty."""
    cached = read_json(LOFTY_PORTFOLIO_CACHE)
    if cached and cached.get("_cached_at"):
        age_hours = (datetime.now(timezone.utc) - datetime.fromisoformat(
            cached["_cached_at"].replace("Z", "+00:00")
        )).total_seconds() / 3600
        if age_hours < 6:
            return cached
    return {}


def parse_ledger(ledger_path: Path) -> dict[str, Any]:
    """Parse the ECO Systems General Ledger CSV and extract property-level financials."""
    properties: dict[str, dict[str, float]] = defaultdict(lambda: {
        "income": 0.0,
        "expenses": 0.0,
        "transfers_in": 0.0,
        "transfers_out": 0.0,
        "transaction_count": 0,
    })
    total_income = 0.0
    total_expenses = 0.0
    total_transfers_in = 0.0
    total_transfers_out = 0.0
    total_transactions = 0
    date_min: str | None = None
    date_max: str | None = None

    try:
        with ledger_path.open("r", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                date_str = (row.get("Date") or "").strip()
                if not date_str:
                    continue
                if not date_min or date_str < date_min:
                    date_min = date_str
                if not date_max or date_str > date_max:
                    date_max = date_str

                amount_str = (row.get("Amount") or "0").strip()
                try:
                    amount = float(amount_str)
                except ValueError:
                    continue

                tx_type = (row.get("Type") or "").strip().lower()
                category = (row.get("Category") or "").strip()
                property_name = (row.get("Property") or "").strip()
                if not property_name:
                    property_name = "Unassigned"

                total_transactions += 1
                prop = properties[property_name]
                prop["transaction_count"] += 1

                if tx_type == "transaction":
                    if amount > 0:
                        prop["income"] += amount
                        total_income += amount
                    else:
                        prop["expenses"] += abs(amount)
                        total_expenses += abs(amount)
                elif "transfer" in tx_type:
                    if amount > 0:
                        prop["transfers_in"] += amount
                        total_transfers_in += amount
                    else:
                        prop["transfers_out"] += abs(amount)
                        total_transfers_out += abs(amount)
    except OSError as e:
        return {"error": str(e)}

    # Compute net cash flow per property
    property_summaries = []
    for name, data in sorted(properties.items()):
        net_cf = data["income"] - data["expenses"]
        property_summaries.append({
            "property": name,
            "income": round(data["income"], 2),
            "expenses": round(data["expenses"], 2),
            "net_cash_flow": round(net_cf, 2),
            "transfers_in": round(data["transfers_in"], 2),
            "transfers_out": round(data["transfers_out"], 2),
            "transaction_count": data["transaction_count"],
        })

    return {
        "ledger_path": str(ledger_path),
        "date_range": {"start": date_min, "end": date_max},
        "totals": {
            "income": round(total_income, 2),
            "expenses": round(total_expenses, 2),
            "net_cash_flow": round(total_income - total_expenses, 2),
            "transfers_in": round(total_transfers_in, 2),
            "transfers_out": round(total_transfers_out, 2),
            "transaction_count": total_transactions,
        },
        "property_count": len(properties),
        "properties": property_summaries,
    }


def build_summary(ledger_data: dict[str, Any], lofty_data: dict[str, Any]) -> dict[str, Any]:
    """Build the combined investor financial summary."""
    now = iso_z()

    # Lofty portfolio metrics
    lofty_total_value = float(lofty_data.get("totalValueUsd") or 0)
    lofty_total_invested = float(lofty_data.get("totalInvestedUsd") or 0)
    lofty_daily_income = float(lofty_data.get("dailyIncomeUsd") or 0)
    lofty_annual_income = float(lofty_data.get("annualIncomeUsd") or 0)
    lofty_avg_yield = float(lofty_data.get("averageYieldPercent") or 0)
    lofty_property_count = int(lofty_data.get("propertyCount") or 0)
    lofty_owned = lofty_data.get("ownedProperties") or []

    # Categorize Lofty properties
    active_props = [p for p in lofty_owned if p.get("property", {}).get("listingStatus") == "Active"]
    archived_props = [p for p in lofty_owned if p.get("property", {}).get("listingStatus") == "Archived"]

    # Top performers by annual cash flow
    sorted_by_cf = sorted(lofty_owned, key=lambda p: float(p.get("property", {}).get("annualCashFlowUsd") or 0), reverse=True)
    top_performers = [
        {
            "address": p.get("property", {}).get("address", ""),
            "tokens": p.get("ownedQuantity", 0),
            "value_usd": round(float(p.get("currentValueUsd") or 0), 2),
            "annual_cf_usd": round(float(p.get("property", {}).get("annualCashFlowUsd") or 0), 2),
            "coc_yield": round(float(p.get("property", {}).get("cocYieldPercent") or 0), 2),
        }
        for p in sorted_by_cf[:5]
        if float(p.get("property", {}).get("annualCashFlowUsd") or 0) > 0
    ]

    # Baselane ledger metrics
    ledger_totals = ledger_data.get("totals", {})
    ledger_date_range = ledger_data.get("date_range", {})

    # Property-level Baselane breakdown (top 10 by net cash flow)
    baselane_props = ledger_data.get("properties", [])
    baselane_top = sorted(baselane_props, key=lambda x: x.get("net_cash_flow", 0), reverse=True)[:10]

    summary = {
        "generated_at": now,
        "period": {
            "ledger_start": ledger_date_range.get("start"),
            "ledger_end": ledger_date_range.get("end"),
        },
        "lofty": {
            "total_portfolio_value_usd": round(lofty_total_value, 2),
            "total_invested_usd": round(lofty_total_invested, 2),
            "unrealized_gain_loss_usd": round(lofty_total_value - lofty_total_invested, 2),
            "daily_income_usd": round(lofty_daily_income, 6),
            "annual_income_usd": round(lofty_annual_income, 2),
            "average_yield_percent": round(lofty_avg_yield, 2),
            "total_property_count": lofty_property_count,
            "active_count": len(active_props),
            "archived_count": len(archived_props),
            "top_performers": top_performers,
        },
        "baselane": {
            "total_income_usd": ledger_totals.get("income", 0),
            "total_expenses_usd": ledger_totals.get("expenses", 0),
            "net_cash_flow_usd": ledger_totals.get("net_cash_flow", 0),
            "transfers_in_usd": ledger_totals.get("transfers_in", 0),
            "transfers_out_usd": ledger_totals.get("transfers_out", 0),
            "transaction_count": ledger_totals.get("transaction_count", 0),
            "property_count": ledger_data.get("property_count", 0),
            "top_properties_by_cash_flow": baselane_top,
        },
        "combined": {
            "total_property_count": max(lofty_property_count, ledger_data.get("property_count", 0)),
            "lofty_portfolio_value_usd": round(lofty_total_value, 2),
            "baselane_net_cash_flow_usd": ledger_totals.get("net_cash_flow", 0),
        },
    }

    return summary


def format_markdown(summary: dict[str, Any]) -> str:
    """Format the summary as a clean, investor-facing Markdown document."""
    gen_date = summary.get("generated_at", "")
    gen_date_display = datetime.fromisoformat(gen_date.replace("Z", "+00:00")).strftime("%B %d, %Y") if gen_date else ""

    lofty = summary.get("lofty", {})
    baselane = summary.get("baselane", {})
    period = summary.get("period", {})

    lines = []
    lines.append(f"## ECO Systems DAO — Investor Financial Summary")
    lines.append(f"**Generated:** {gen_date_display}")
    lines.append("")

    # Portfolio overview
    lines.append("### Portfolio Overview (Lofty)")
    lines.append(f"- **Total portfolio value:** ${lofty.get('total_portfolio_value_usd', 0):,.2f}")
    lines.append(f"- **Total invested:** ${lofty.get('total_invested_usd', 0):,.2f}")
    lines.append(f"- **Unrealized gain/loss:** ${lofty.get('unrealized_gain_loss_usd', 0):,.2f}")
    lines.append(f"- **Daily income:** ${lofty.get('daily_income_usd', 0):,.4f}")
    lines.append(f"- **Annual income (projected):** ${lofty.get('annual_income_usd', 0):,.2f}")
    lines.append(f"- **Average yield:** {lofty.get('average_yield_percent', 0):.2f}%")
    lines.append(f"- **Properties:** {lofty.get('active_count', 0)} active, {lofty.get('archived_count', 0)} archived ({lofty.get('total_property_count', 0)} total)")
    lines.append("")

    # Top performers
    top_performers = lofty.get("top_performers", [])
    if top_performers:
        lines.append("### Top Performing Properties (by annual cash flow)")
        for p in top_performers:
            lines.append(f"- **{p['address']}** — {p['tokens']} token(s), ${p['value_usd']:,.2f} value, ${p['annual_cf_usd']:,.2f}/yr, {p['coc_yield']}% CoC")
        lines.append("")

    # Baselane financial activity
    lines.append("### Baselane Financial Activity")
    if period.get("ledger_start"):
        lines.append(f"- **Ledger period:** {period['ledger_start']} to {period.get('ledger_end', 'present')}")
    lines.append(f"- **Total income:** ${baselane.get('total_income_usd', 0):,.2f}")
    lines.append(f"- **Total expenses:** ${baselane.get('total_expenses_usd', 0):,.2f}")
    lines.append(f"- **Net cash flow:** ${baselane.get('net_cash_flow_usd', 0):,.2f}")
    lines.append(f"- **Transfers in:** ${baselane.get('transfers_in_usd', 0):,.2f}")
    lines.append(f"- **Transfers out:** ${baselane.get('transfers_out_usd', 0):,.2f}")
    lines.append(f"- **Transactions:** {baselane.get('transaction_count', 0):,}")
    lines.append(f"- **Properties tracked:** {baselane.get('property_count', 0)}")
    lines.append("")

    # Top Baselane properties
    baselane_top = baselane.get("top_properties_by_cash_flow", [])
    if baselane_top:
        lines.append("### Top Properties by Net Cash Flow (Baselane)")
        for p in baselane_top[:5]:
            lines.append(f"- **{p['property']}** — Income: ${p['income']:,.2f}, Expenses: ${p['expenses']:,.2f}, Net: ${p['net_cash_flow']:,.2f} ({p['transaction_count']} txns)")
        lines.append("")

    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def append_to_updates(summary_md: str, summary: dict[str, Any]) -> bool:
    """Append the summary to UPDATES.md."""
    if not UPDATES_MD.exists():
        UPDATES_MD.write_text("# UPDATES.md — Investor Updates Log\n\n", encoding="utf-8")

    existing = UPDATES_MD.read_text(encoding="utf-8")
    gen_date = summary.get("generated_at", "")
    date_header = datetime.fromisoformat(gen_date.replace("Z", "+00:00")).strftime("%Y-%m-%d") if gen_date else datetime.now().strftime("%Y-%m-%d")

    # Avoid duplicate entries for the same date
    if f"## ECO Systems DAO — Investor Financial Summary" in existing and date_header in existing:
        # Replace existing entry for this date
        pattern = re.compile(
            r"## ECO Systems DAO — Investor Financial Summary.*?(?=\n## |\Z)",
            re.DOTALL
        )
        if pattern.search(existing):
            existing = pattern.sub(summary_md, existing)
        else:
            existing = existing.rstrip() + "\n\n" + summary_md
    else:
        existing = existing.rstrip() + "\n\n" + summary_md

    UPDATES_MD.write_text(existing, encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate investor financial summary")
    parser.add_argument("--append-updates", action="store_true", help="Append summary to UPDATES.md")
    parser.add_argument("--json-only", action="store_true", help="Only output JSON report")
    parser.add_argument("--lofty-cache", type=str, help="Path to Lofty portfolio cache JSON")
    args = parser.parse_args()

    # Load ledger
    ledger_path = find_ledger()
    if not ledger_path:
        print("ERROR: ECO Systems General Ledger CSV not found", file=sys.stderr)
        return 1

    print(f"[investor-summary] Loading ledger from {ledger_path}...", file=sys.stderr)
    ledger_data = parse_ledger(ledger_path)
    if ledger_data.get("error"):
        print(f"ERROR: Failed to parse ledger: {ledger_data['error']}", file=sys.stderr)
        return 1

    # Load Lofty portfolio cache
    lofty_cache_path = Path(args.lofty_cache) if args.lofty_cache else LOFTY_PORTFOLIO_CACHE
    lofty_data = read_json(lofty_cache_path)
    if not lofty_data:
        print(f"[investor-summary] No Lofty portfolio cache found at {lofty_cache_path}", file=sys.stderr)
        print("[investor-summary] Run loftyassist__get_portfolio_summary and cache it first", file=sys.stderr)
        # Continue with empty Lofty data — summary will still have Baselane data

    # Build summary
    summary = build_summary(ledger_data, lofty_data)

    # Write JSON
    SUMMARY_JSON.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"[investor-summary] JSON report written to {SUMMARY_JSON}", file=sys.stderr)

    if args.json_only:
        print(json.dumps(summary, indent=2))
        return 0

    # Write Markdown
    summary_md = format_markdown(summary)
    SUMMARY_MD.write_text(summary_md, encoding="utf-8")
    print(f"[investor-summary] Markdown report written to {SUMMARY_MD}", file=sys.stderr)

    # Append to UPDATES.md
    if args.append_updates:
        if append_to_updates(summary_md, summary):
            print(f"[investor-summary] Appended to {UPDATES_MD}", file=sys.stderr)
        else:
            print(f"[investor-summary] Failed to append to {UPDATES_MD}", file=sys.stderr)

    # Print summary to stdout
    print(summary_md)
    return 0


if __name__ == "__main__":
    sys.exit(main())