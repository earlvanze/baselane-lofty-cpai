#!/usr/bin/env python3
"""Analyze National Grid bills for charge, payment, ESCO-rate, and deferral anomalies."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

MONEY = r"-?\s*\$?\s*([0-9][0-9,]*\.\d{2})"
CACHE_VERSION = 2
MONTHS = {name: number for number, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"), 1
)}


@dataclass
class Bill:
    property: str
    source: str
    source_kind: str
    account: str | None = None
    bill_date: str | None = None
    service_end: str | None = None
    current_charges: float | None = None
    amount_due: float | None = None
    payment_received: float | None = None
    budget_payment: float | None = None
    deferred_balance: float | None = None
    electric_usage_kwh: float | None = None
    gas_usage_therms: float | None = None
    electric_supply_rate: float | None = None
    gas_supply_rate: float | None = None
    supplier: str | None = None


def amount(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value.replace("$", "").replace(",", "").replace(" ", "")), 4)
    except ValueError:
        return None


def iso_date(value: str) -> str | None:
    value = value.strip().replace(",", "")
    for fmt in ("%b %d %Y", "%B %d %Y", "%b %Y", "%B %Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def line_last_amount(text: str, label: str) -> float | None:
    for line in text.splitlines():
        if label.lower() in line.lower():
            values = re.findall(r"-?\s*\$?\s*([0-9][0-9,]*\.\d{2})", line)
            if values:
                return amount(values[-1])
    return None


def line_amount_after_label(text: str, label: str) -> float | None:
    for line in text.splitlines():
        match = re.search(re.escape(label) + r"\s+" + MONEY, line, re.I)
        if match:
            return amount(match.group(1))
    return None


def parse_pdf(path: Path, property_name: str, cache_dir: Path | None = None) -> Bill | None:
    cache_path = None
    if cache_dir:
        stat = path.stat()
        cache_key = hashlib.sha256(f"v{CACHE_VERSION}:{path}:{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()
        cache_path = cache_dir / f"{cache_key}.json"
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            return Bill(**cached) if cached else None
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    try:
        proc = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, timeout=45, check=False
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    text = proc.stdout
    if proc.returncode or "NATIONAL GRID" not in text.upper() or "BILLING PERIOD" not in text.upper():
        return None
    first_page = text.split("\f", 1)[0]
    account_match = re.search(r"ACCOUNT NUMBER\s+([0-9-]{8,})", first_page, re.I | re.S)
    billing_match = re.search(
        r"([A-Z][a-z]{2,8}\s+\d{1,2},?\s+\d{4})\s+to\s+([A-Z][a-z]{2,8}\s+\d{1,2},?\s+\d{4})",
        first_page,
        re.I,
    )
    issued_match = re.search(r"(20\d{2})[-_](\d{2})[-_](\d{2})", path.name)
    supplier_match = re.search(r"^\s*SUPPLIER\s+([^\n]+)", text, re.I | re.M)

    current = line_last_amount(first_page, "Total Current Charges") or line_last_amount(first_page, "Current Charges")
    budget = line_amount_after_label(first_page, "Budget Plan Amount")
    payment = line_last_amount(first_page, "Payment Received")
    if payment == 0:
        payment = 0.0
    due = line_amount_after_label(first_page, "Amount Due")
    deferred = line_last_amount(first_page, "Amount Due Company")
    if deferred is None:
        actual = line_last_amount(first_page, "Accumulated Actual Charges")
        budget_accum = line_last_amount(first_page, "Accumulated Budget Plan charges")
        if actual is not None and budget_accum is not None:
            deferred = round(max(actual - budget_accum, 0), 2)
    if deferred is None:
        deferred = line_last_amount(first_page, "Payment Agreement balance")

    electric = re.search(r"Electricity Supply\s+.*?([0-9]+\.[0-9]+)\s+x\s+([0-9,]+)\s+kWh", text, re.I | re.S)
    gas = re.search(r"Gas Supply\s+.*?([0-9]+\.[0-9]+)\s+x\s+([0-9,]+)\s+therms", text, re.I | re.S)
    bill = Bill(
        property=property_name,
        source=str(path),
        source_kind="national_grid_pdf",
        account=account_match.group(1).replace("-", "") if account_match else None,
        bill_date=(f"{issued_match.group(1)}-{issued_match.group(2)}-{issued_match.group(3)}" if issued_match else None),
        service_end=iso_date(billing_match.group(2)) if billing_match else None,
        current_charges=current,
        amount_due=due,
        payment_received=payment,
        budget_payment=budget,
        deferred_balance=deferred,
        electric_usage_kwh=amount(electric.group(2)) if electric else None,
        electric_supply_rate=amount(electric.group(1)) if electric else None,
        gas_usage_therms=amount(gas.group(2)) if gas else None,
        gas_supply_rate=amount(gas.group(1)) if gas else None,
        supplier=supplier_match.group(1).strip() if supplier_match else None,
    )
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(asdict(bill)), encoding="utf-8")
    return bill


def parse_arcadia_csv(path: Path, property_name: str) -> list[Bill]:
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or "statementDate" not in reader.fieldnames:
                return []
            rows = []
            for row in reader:
                if "national grid" not in (row.get("utility") or "").lower():
                    continue
                rows.append(Bill(
                    property=property_name,
                    source=str(path),
                    source_kind="arcadia_csv",
                    account=(row.get("accountNumber") or row.get("utilityAccountId") or "").replace("-", "") or None,
                    bill_date=iso_date(row.get("statementDate") or ""),
                    service_end=iso_date(row.get("serviceEndDate") or ""),
                    current_charges=amount(row.get("utilityCharge")),
                    amount_due=amount(row.get("amountDue")),
                    electric_usage_kwh=amount(row.get("kwh")),
                    supplier=(row.get("planName") or None),
                ))
            return rows
    except (OSError, csv.Error):
        return []


def parse_national_grid_csv(path: Path, property_name: str) -> list[Bill]:
    try:
        lines = list(csv.reader(path.open(newline="", encoding="utf-8-sig")))
    except (OSError, csv.Error):
        return []
    account = None
    section = None
    header: list[str] | None = None
    results: dict[tuple[str, str], Bill] = {}
    for row in lines:
        if not row:
            continue
        label = row[0].strip()
        if label in {"Electric Data", "Gas Data"}:
            section = label
            header = None
        elif label == "Account Number:" and len(row) > 1:
            account = row[1].replace("-", "").strip()
        elif label == "Bill Date":
            header = row
        elif header and section and len(row) >= len(header):
            record = dict(zip(header, row))
            bill_date = iso_date(record.get("Bill Date", ""))
            if not bill_date:
                continue
            key = (account or "unknown", bill_date)
            bill = results.setdefault(key, Bill(property_name, str(path), "national_grid_csv", account=account, bill_date=bill_date, service_end=bill_date))
            if section == "Electric Data":
                bill.electric_usage_kwh = amount(record.get("Usage (kWh)"))
            else:
                bill.gas_usage_therms = amount(record.get("Usage (Therms)"))
            cost = amount(record.get("Cost ($)"))
            if cost is not None:
                bill.current_charges = round((bill.current_charges or 0) + cost, 2)
    return list(results.values())


def discover(property_cfg: dict[str, Any], root: Path, cache_dir: Path | None = None) -> tuple[list[Bill], list[str]]:
    bills: list[Bill] = []
    issues: list[str] = []
    seen_signatures: set[tuple[str, int, str]] = set()
    files: list[Path] = []
    for relative in property_cfg["source_dirs"]:
        source_dir = root / relative
        if not source_dir.exists():
            issues.append(f"missing source directory: {source_dir}")
            continue
        files.extend(path for path in source_dir.rglob("*") if path.is_file() and path.suffix.lower() in {".pdf", ".csv"})
    pdf_paths: list[Path] = []
    for path in sorted(set(files), key=lambda item: (len(str(item)), str(item))):
        try:
            stat = path.stat()
        except OSError as exc:
            issues.append(f"unreadable source: {path}: {exc}")
            continue
        filename_date = re.search(r"20\d{2}[-_]\d{2}[-_]\d{2}", path.name)
        signature = (path.suffix.lower(), stat.st_size, filename_date.group(0) if filename_date else path.stem.split(" - ", 1)[0])
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        if path.suffix.lower() == ".pdf":
            pdf_paths.append(path)
        else:
            parsed = parse_arcadia_csv(path, property_cfg["name"])
            bills.extend(parsed or parse_national_grid_csv(path, property_cfg["name"]))
    if pdf_paths:
        with ThreadPoolExecutor(max_workers=min(4, len(pdf_paths))) as pool:
            parsed_pdfs = pool.map(lambda path: parse_pdf(path, property_cfg["name"], cache_dir), pdf_paths)
            bills.extend(bill for bill in parsed_pdfs if bill)
    # Prefer PDF detail over CSV for the same account/date.
    unique: dict[tuple[str, str], Bill] = {}
    for bill in sorted(bills, key=lambda item: item.source_kind != "national_grid_pdf"):
        key = (bill.account or Path(bill.source).name, bill.bill_date or bill.service_end or bill.source)
        unique.setdefault(key, bill)
    return sorted(unique.values(), key=lambda item: (item.account or "", item.bill_date or item.service_end or "")), issues


def percent_change(previous: float, current: float) -> float | None:
    return round((current - previous) / previous * 100, 2) if previous else None


def public_account_key(account: str | None) -> str:
    """Return a stable account reference without disclosing the account number."""
    if not account:
        return "acct-unknown"
    return f"acct-{hashlib.sha256(account.encode()).hexdigest()[:10]}"


def build_apg_portfolio_review(properties: list[dict[str, Any]], from_month: str | None) -> dict[str, Any]:
    """Build a factual, account-safe APG timeline for every configured property."""
    accounts: list[dict[str, Any]] = []
    for prop in properties:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for bill in prop["bills"]:
            bill_date = bill.get("bill_date") or bill.get("service_end")
            if not bill_date or (from_month and bill_date[:7] < from_month):
                continue
            if bill.get("source_kind") != "national_grid_pdf" or "american power" not in (bill.get("supplier") or "").lower():
                continue
            grouped.setdefault(bill.get("account") or "unknown", []).append(bill)
        for account, bills in grouped.items():
            bills.sort(key=lambda item: item.get("bill_date") or item.get("service_end") or "")
            anomalies = [
                item for item in prop["anomalies"]
                if item.get("account") == account and (
                    item.get("apg_disputed_variable_rate_pattern")
                    or item.get("type") in {"active_apg_deferred_balance", "supplier_rate_increase"}
                )
            ]
            timeline = [
                {
                    "bill_date": bill.get("bill_date") or bill.get("service_end"),
                    "payment": bill.get("payment_received") if bill.get("payment_received") is not None else bill.get("budget_payment"),
                    "current_charges": bill.get("current_charges"),
                    "deferred_balance": bill.get("deferred_balance"),
                    "electric_supply_rate": bill.get("electric_supply_rate"),
                    "gas_supply_rate": bill.get("gas_supply_rate"),
                }
                for bill in bills
            ]
            fixed = [item for item in anomalies if item.get("apg_disputed_variable_rate_pattern")]
            accounts.append({
                "property": prop["name"],
                "account": public_account_key(account),
                "statement_count": len(bills),
                "first_statement_date": timeline[0]["bill_date"],
                "latest_statement_date": timeline[-1]["bill_date"],
                "first_observable_fixed_payment_divergence": min((item.get("bill_date") for item in fixed if item.get("bill_date")), default=None),
                "fixed_payment_divergence_count": len(fixed),
                "critical_alert_count": sum(item.get("severity") == "critical" for item in anomalies),
                "latest_statement": timeline[-1],
                "timeline": timeline,
            })
    accounts.sort(key=lambda item: (item["property"], item["account"]))
    return {
        "scope": "American Power & Gas supplier statements in configured National Grid properties",
        "legal_note": "Statement facts and anomaly indicators only; not a legal conclusion.",
        "account_count": len(accounts),
        "property_count": len({item["property"] for item in accounts}),
        "fixed_payment_divergence_count": sum(item["fixed_payment_divergence_count"] for item in accounts),
        "accounts": accounts,
    }


def analyze_property(
    name: str,
    public_dir: str,
    bills: list[Bill],
    thresholds: dict[str, float],
    as_of: date,
    from_month: str | None = None,
) -> dict[str, Any]:
    anomalies: list[dict[str, Any]] = []
    by_account: dict[str, list[Bill]] = {}
    for bill in bills:
        by_account.setdefault(bill.account or "unknown", []).append(bill)
    for account, records in by_account.items():
        records.sort(key=lambda item: item.bill_date or item.service_end or "")
        for previous, current in zip(records, records[1:]):
            current_date = current.bill_date or current.service_end
            if from_month and (not current_date or current_date[:7] < from_month):
                continue
            charge_delta = None
            charge_pct = None
            if previous.current_charges is not None and current.current_charges is not None:
                charge_delta = round(current.current_charges - previous.current_charges, 2)
                charge_pct = percent_change(previous.current_charges, current.current_charges)
                if charge_delta >= thresholds["charge_increase_dollars"] and (charge_pct or 0) >= thresholds["charge_increase_percent"]:
                    anomalies.append({"type": "current_charges_increase", "severity": "warning", "account": account, "bill_date": current_date, "previous": previous.current_charges, "current": current.current_charges, "delta": charge_delta, "percent": charge_pct})
            for fuel, field in (("electric", "electric_supply_rate"), ("gas", "gas_supply_rate")):
                old_rate, new_rate = getattr(previous, field), getattr(current, field)
                rate_pct = percent_change(old_rate, new_rate) if old_rate is not None and new_rate is not None else None
                if rate_pct is not None and rate_pct >= thresholds["supplier_rate_increase_percent"]:
                    anomalies.append({"type": "supplier_rate_increase", "severity": "critical" if "american power" in (current.supplier or "").lower() else "warning", "account": account, "bill_date": current_date, "fuel": fuel, "supplier": current.supplier, "previous": old_rate, "current": new_rate, "percent": rate_pct})

        # Deferral analysis requires statement-level plan/payment fields. Keep
        # CSV usage rows from interrupting the statement-to-statement sequence.
        detailed = [item for item in records if item.source_kind == "national_grid_pdf" and item.deferred_balance is not None]
        for previous, current in zip(detailed, detailed[1:]):
            if from_month and (not current.bill_date or current.bill_date[:7] < from_month):
                continue
            if previous.current_charges is None or current.current_charges is None:
                continue
            charge_delta = round(current.current_charges - previous.current_charges, 2)
            old_payment = previous.payment_received if previous.payment_received is not None else previous.budget_payment
            new_payment = current.payment_received if current.payment_received is not None else current.budget_payment
            deferred_delta = round(current.deferred_balance - previous.deferred_balance, 2)
            stable_payment = (
                old_payment is not None and new_payment is not None
                and abs(new_payment - old_payment) <= max(thresholds["payment_stable_dollars"], abs(old_payment) * thresholds["payment_stable_percent"] / 100)
            )
            if charge_delta >= thresholds["charge_increase_dollars"] and stable_payment and deferred_delta >= thresholds["deferred_balance_increase_dollars"]:
                supplier = current.supplier or previous.supplier
                anomalies.append({
                    "type": "fixed_payment_rising_charges_deferred_balance",
                    "severity": "critical",
                    "account": account,
                    "bill_date": current.bill_date,
                    "supplier": supplier,
                    "payment_previous": old_payment,
                    "payment_current": new_payment,
                    "charges_previous": previous.current_charges,
                    "charges_current": current.current_charges,
                    "deferred_previous": previous.deferred_balance,
                    "deferred_current": current.deferred_balance,
                    "deferred_delta": deferred_delta,
                    "apg_disputed_variable_rate_pattern": "american power" in (supplier or "").lower(),
                })

    dated = [bill for bill in bills if bill.bill_date or bill.service_end]
    latest = max(dated, key=lambda item: item.bill_date or item.service_end) if dated else None
    latest_date = date.fromisoformat((latest.bill_date or latest.service_end)) if latest else None
    days_old = (as_of - latest_date).days if latest_date else None
    if days_old is None or days_old > thresholds["stale_after_days"]:
        anomalies.append({"type": "stale_or_missing_bill_evidence", "severity": "warning", "bill_date": latest_date.isoformat() if latest_date else None, "days_old": days_old})
    for account, account_bills in by_account.items():
        detailed_bills = [bill for bill in account_bills if bill.source_kind == "national_grid_pdf" and bill.deferred_balance is not None]
        latest_detailed = max(detailed_bills, key=lambda item: item.bill_date or item.service_end or "") if detailed_bills else None
        if latest_detailed and latest_detailed.deferred_balance and latest_detailed.deferred_balance >= thresholds["deferred_balance_increase_dollars"] and "american power" in (latest_detailed.supplier or "").lower():
            anomalies.append({"type": "active_apg_deferred_balance", "severity": "critical", "account": account, "bill_date": latest_detailed.bill_date, "supplier": latest_detailed.supplier, "deferred_balance": latest_detailed.deferred_balance, "current_charges": latest_detailed.current_charges, "payment_received": latest_detailed.payment_received, "budget_payment": latest_detailed.budget_payment})

    critical = [item for item in anomalies if item["severity"] == "critical"]
    if critical:
        headline = f"{len(critical)} critical National Grid/APG billing alert(s); review variable supply rates, payments, and deferred balances."
    elif anomalies:
        headline = f"{len(anomalies)} National Grid billing warning(s) require review."
    else:
        headline = "No configured month-over-month National Grid anomaly detected."
    apg_fixed = [item for item in anomalies if item["type"] == "fixed_payment_rising_charges_deferred_balance" and item.get("apg_disputed_variable_rate_pattern")]
    active_apg = [item for item in anomalies if item["type"] == "active_apg_deferred_balance"]
    if apg_fixed:
        item = sorted(apg_fixed, key=lambda entry: entry.get("bill_date") or "")[-1]
        owner_summary = (
            "Disputed American Power & Gas variable-rate pattern: payment held at "
            f"${item['payment_current']:,.2f} while monthly charges rose from ${item['charges_previous']:,.2f} "
            f"to ${item['charges_current']:,.2f}, and the deferred balance grew from "
            f"${item['deferred_previous']:,.2f} to ${item['deferred_current']:,.2f} (+${item['deferred_delta']:,.2f})."
        )
        if active_apg:
            balances = ", ".join(f"${item['deferred_balance']:,.2f}" for item in active_apg)
            owner_summary += f" Latest statement-level APG deferred balance(s): {balances}."
    else:
        owner_summary = headline
    return {
        "name": name,
        "public_dir": public_dir,
        "status": "critical" if critical else ("review" if anomalies else "ok"),
        "headline": headline,
        "owner_summary": owner_summary,
        "bill_count": sum(not from_month or (bill.bill_date or bill.service_end or "")[:7] >= from_month for bill in bills),
        "latest_bill_date": latest_date.isoformat() if latest_date else None,
        "anomalies": anomalies,
        "bills": [asdict(bill) for bill in bills],
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [f"# National Grid Bill Anomaly Report: {report['run_month']}", "", f"Status: **{report['status']}**", f"Generated: {report['generated_at']}", ""]
    for prop in report["properties"]:
        lines.extend([f"## {prop['name']}", "", f"- Status: **{prop['status']}**", f"- Bills analyzed: {prop['bill_count']}", f"- Latest bill: {prop['latest_bill_date'] or 'missing'}", f"- Summary: {prop['headline']}"])
        for anomaly in prop["anomalies"]:
            detail = json.dumps(anomaly, sort_keys=True, separators=(", ", ": "))
            lines.append(f"- `{anomaly['type']}` ({anomaly['severity']}): {detail}")
        lines.append("")
    apg = report["apg_portfolio_review"]
    lines.extend(["## American Power & Gas Portfolio Review", "", f"- APG accounts: {apg['account_count']} across {apg['property_count']} property/properties", f"- Fixed-payment / rising-charge / deferred-balance divergences: {apg['fixed_payment_divergence_count']}"])
    for account in apg["accounts"]:
        lines.append(
            f"- {account['property']} / {account['account']}: {account['statement_count']} APG statement(s), "
            f"first divergence: {account['first_observable_fixed_payment_divergence'] or 'not detected'}, "
            f"latest: {account['latest_statement_date']}."
        )
    lines.append("")
    lines.extend(["## Interpretation", "", "The fixed-payment alert fires only when consumption charges rise, the payment remains stable, and the deferred balance also grows. APG references identify the disputed American Power & Gas variable-rate pattern for review; they are not a legal conclusion.", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-estate-root", required=True, type=Path)
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "config/national_grid_bill_analyzer.json")
    parser.add_argument("--month", required=True, help="Reporting month, YYYY-MM")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--markdown", required=True, type=Path)
    parser.add_argument("--cache-dir", type=Path, help="PDF parse cache (default: <report-dir>/.national-grid-cache)")
    args = parser.parse_args()
    try:
        as_of = datetime.strptime(args.month + "-01", "%Y-%m-%d").date()
        if as_of.month == 12:
            as_of = date(as_of.year + 1, 1, 1)
        else:
            as_of = date(as_of.year, as_of.month + 1, 1)
        config = json.loads(args.config.read_text(encoding="utf-8"))
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"national-grid analyzer configuration error: {exc}", file=sys.stderr)
        return 2
    properties = []
    source_issues: list[str] = []
    cache_dir = args.cache_dir or args.report.parent / ".national-grid-cache"
    for prop_cfg in config["properties"]:
        bills, issues = discover(prop_cfg, args.real_estate_root, cache_dir)
        source_issues.extend(f"{prop_cfg['name']}: {issue}" for issue in issues)
        properties.append(analyze_property(
            prop_cfg["name"], prop_cfg["public_dir"], bills, config["thresholds"], as_of,
            config.get("analysis_from_month"),
        ))
    statuses = {prop["status"] for prop in properties}
    status = "critical" if "critical" in statuses else ("review" if "review" in statuses or source_issues else "ok")
    report = {
        "schema_version": 2,
        "run_month": args.month,
        "analysis_from_month": config.get("analysis_from_month"),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": status,
        "property_count": len(properties),
        "anomaly_count": sum(len(prop["anomalies"]) for prop in properties),
        "critical_count": sum(sum(item["severity"] == "critical" for item in prop["anomalies"]) for prop in properties),
        "source_issues": source_issues,
        "properties": properties,
        "apg_portfolio_review": build_apg_portfolio_review(properties, config.get("analysis_from_month")),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    args.markdown.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "property_count", "anomaly_count", "critical_count")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
