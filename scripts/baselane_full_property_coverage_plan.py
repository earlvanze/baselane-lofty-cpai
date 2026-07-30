#!/usr/bin/env python3
"""Build a deterministic property/category plan for every unassigned Baselane row."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable


ROOT = Path("/home/digit/.openclaw/workspace")
REPORTS = ROOT / "reports"
DROPBOX_REPORTS = Path(
    "/mnt/c/Users/digit/Dropbox/Projects/cyber-gateway/config/openclaw/workspace/reports"
)
GENERIC_PROPERTY = "Mining, Sales, Consulting, and PM"
GENERIC_PROPERTY_ID = "37648"
EXCLUDED_PROPERTIES = {"1 Coolwood Dr.", "Coolwood"}
EXCLUDED_TEXT_PATTERNS = {
    "coolwood_other_workspace": re.compile(r"\bCOOLWOOD\b", re.I),
}

PROPERTY_TEXT_RULES = [
    ("724_3rd", re.compile(r"\b724\s+3RD\b", re.I), "724 3rd Ave"),
    ("82_madison", re.compile(r"\b82\s+MADISON\b", re.I), "82 Madison Ave"),
    ("84_madison", re.compile(r"\b84\s+MADISON\b", re.I), "84 Madison Ave"),
    ("86_madison", re.compile(r"\b86\s+MADISON\b", re.I), "86 Madison Ave"),
    ("88_madison", re.compile(r"\b88\s+MADISON\b", re.I), "88 Madison Ave"),
    ("90_madison", re.compile(r"\b90\s+MADISON\b", re.I), "90 Madison Ave"),
]

PERSONAL_PATTERNS = {
    "grocery": re.compile(
        r"\b(ALDI|CARREFOUR|GIANT EAGLE|GROCERY|KROGER|LIDL|MERCADONA|SAFEWAY|"
        r"SUPERMARKET|TRADER JOE|WHOLE FOODS)\b",
        re.I,
    ),
    "pharmacy": re.compile(
        r"\b(APOTHEK\w*|CVS|FARMAC\w*|PHARM\w*|RITE AID|WALGREENS)\b",
        re.I,
    ),
    "remitly": re.compile(r"\bREMITLY\b", re.I),
    "bunq": re.compile(r"\bBUNQ\b", re.I),
}

CATEGORY_RULES = [
    ("management_fee", re.compile(r"\b(PM FEE|PROPERTY MANAGEMENT FEE)\b", re.I), "Management Fees"),
    (
        "utilities",
        re.compile(
            r"\b(FPL|FIRSTENERGY|ELECTRIC|ENERGY|GAS BILL|SEWER|WATER BILL|UTILITY|UTILITIES)\b",
            re.I,
        ),
        "Utilities",
    ),
    (
        "government_filing",
        re.compile(r"\b(SECRETARY OF STATE|IL SECRETARY|WY SECRETARY|CO SECRETARY)\b", re.I),
        "Legal & Other Professional Fees",
    ),
    ("linen", re.compile(r"\bMORGAN LINEN\b", re.I), "Cleaning & Maintenance"),
    (
        "software",
        re.compile(r"\b(DROPBOX|GOOGLE WORKSPACE|MICROSOFT|OPENAI|QUICKBOOKS|SOFTWARE)\b", re.I),
        "Other Operating Expenses",
    ),
    (
        "supplies",
        re.compile(r"\b(AMAZON|HOME DEPOT|LOWE'?S|MENARDS|OFFICE DEPOT|STAPLES)\b", re.I),
        "Supplies",
    ),
    (
        "bank_interest",
        re.compile(r"\b(MONTHLY BANK INTEREST|INTEREST PAYMENT|INTEREST CREDIT)\b", re.I),
        "Interest Received",
    ),
    (
        "mortgage",
        re.compile(r"\b(CITADEL|MORTGAGE|ESCROW\s*-\s*(86|88|90)\s+MADISON)\b", re.I),
        "Mortgage Payments",
    ),
    (
        "transfer",
        re.compile(
            r"\b(CREDIT CARD PAYMENT|INTERNAL TRANSFER|TRANSFER BETWEEN|"
            r"STRAWBERRY TRANSFER|HERON TRANSFER)\b",
            re.I,
        ),
        "Transfers Between Accounts",
    ),
    (
        "professional_services",
        re.compile(r"\b(CONSULTING|ATTORNEY|LEGAL FEE|LAW OFFICE)\b", re.I),
        "Legal & Other Professional Fees",
    ),
]

CATEGORY_TYPES = {
    "Capital Expenditures": "Loan Payments & Capex",
    "Credit Card Interest": "Transfers & Other",
    "Credit Card Payments": "Transfers & Other",
    "Down Payments": "Property Transactions",
    "Escrow Payments": "Transfers & Other",
    "Fees & Other Revenue": "Revenue",
    "Interest Received": "Transfers & Other",
    "Mortgage Disbursements Received": "Property Transactions",
    "Mortgage Interest Payments": "Loan Payments & Capex",
    "Mortgage Payments": "Loan Payments & Capex",
    "Mortgage Principal Payments": "Loan Payments & Capex",
    "Non-Property Expense": "Non-Property Expense",
    "Other": "Transfers & Other",
    "Other Loan Disbursements Received": "Property Transactions",
    "Other Loan Interest Payments": "Loan Payments & Capex",
    "Other Loan Payments": "Loan Payments & Capex",
    "Other Loan Principal Payments": "Loan Payments & Capex",
    "Owner Contributions/Distributions": "Transfers & Other",
    "Rents": "Revenue",
    "Sale Proceeds": "Property Transactions",
    "Security Deposits": "Transfers & Other",
    "Transfers Between Accounts": "Transfers & Other",
}


def norm(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", (value or "").upper()).strip()


def amount_key(value: str) -> str:
    try:
        return f"{Decimal(str(value).replace(',', '').strip()):.2f}"
    except InvalidOperation:
        return str(value).strip()


def row_text(row: dict[str, str]) -> str:
    return " ".join(
        str(row.get(field) or "") for field in ("Merchant", "Description", "Notes")
    )


def personal_reason(row: dict[str, str]) -> str | None:
    text = row_text(row)
    for reason, pattern in PERSONAL_PATTERNS.items():
        if pattern.search(text):
            return reason
    return None


def excluded_reason(row: dict[str, str]) -> str | None:
    text = row_text(row)
    for reason, pattern in EXCLUDED_TEXT_PATTERNS.items():
        if pattern.search(text):
            return reason
    return None


def semantic_category(row: dict[str, str]) -> tuple[str, str] | None:
    text = row_text(row)
    for reason, pattern, category in CATEGORY_RULES:
        if pattern.search(text):
            return category, reason
    return None


def property_history(rows: Iterable[dict[str, str]]) -> dict[str, dict[tuple[str, ...], Counter]]:
    indexes: dict[str, dict[tuple[str, ...], Counter]] = {
        "merchant_description_amount": defaultdict(Counter),
        "merchant_description": defaultdict(Counter),
        "merchant_amount": defaultdict(Counter),
        "merchant": defaultdict(Counter),
    }
    for row in rows:
        prop = str(row.get("Property") or "").strip()
        merchant = norm(row.get("Merchant") or "")
        description = norm(row.get("Description") or "")
        amount = amount_key(row.get("Amount") or "")
        if not prop or not merchant or prop in EXCLUDED_PROPERTIES:
            continue
        indexes["merchant_description_amount"][(merchant, description, amount)][prop] += 1
        indexes["merchant_description"][(merchant, description)][prop] += 1
        indexes["merchant_amount"][(merchant, amount)][prop] += 1
        indexes["merchant"][(merchant,)][prop] += 1
    return indexes


def unique_match(counter: Counter, minimum: int) -> str | None:
    if len(counter) != 1:
        return None
    prop, count = counter.most_common(1)[0]
    return prop if count >= minimum else None


def infer_property(
    row: dict[str, str], indexes: dict[str, dict[tuple[str, ...], Counter]]
) -> tuple[str, str]:
    text = row_text(row)
    if re.search(r"\bECO SYSTEMS LLC PM FEE\b", text, re.I):
        return GENERIC_PROPERTY, "generic_legacy_pm_fee_cleanup"
    for reason, pattern, prop in PROPERTY_TEXT_RULES:
        if pattern.search(text):
            return prop, f"semantic_{reason}"

    merchant = norm(row.get("Merchant") or "")
    description = norm(row.get("Description") or "")
    amount = amount_key(row.get("Amount") or "")
    if not merchant:
        return GENERIC_PROPERTY, "generic_business_fallback"

    tiers = [
        ("merchant_description_amount", (merchant, description, amount), 1),
        ("merchant_description", (merchant, description), 2),
        ("merchant_amount", (merchant, amount), 2),
        ("merchant", (merchant,), 3),
    ]
    for tier, key, minimum in tiers:
        prop = unique_match(indexes[tier].get(key, Counter()), minimum)
        if prop and prop != "Personal":
            return prop, f"historical_{tier}"
    return GENERIC_PROPERTY, "generic_business_fallback"


def infer_category(row: dict[str, str], is_personal: bool) -> tuple[str, str]:
    if is_personal:
        return "Non-Property Expense", "explicit_personal_exception"

    semantic = semantic_category(row)
    if semantic:
        category, reason = semantic
        return category, f"semantic_{reason}"

    current = str(row.get("Category") or "").strip()
    if current and current != "Non-Property Expense":
        return current, "retained_existing_category"

    try:
        amount = Decimal(amount_key(row.get("Amount") or "0"))
    except InvalidOperation:
        amount = Decimal("0")
    if amount >= 0:
        return "Fees & Other Revenue", "generic_revenue_fallback"
    return "Other Operating Expenses", "generic_expense_fallback"


def fingerprint(row: dict[str, str], occurrence: int) -> str:
    parts = [
        row.get("Date") or "",
        amount_key(row.get("Amount") or ""),
        norm(row.get("Merchant") or ""),
        norm(row.get("Description") or ""),
        str(occurrence),
    ]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:20]


def build_plan(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    indexes = property_history(rows)
    cardinalities: Counter = Counter(
        (
            row.get("Date") or "",
            amount_key(row.get("Amount") or ""),
            norm(row.get("Merchant") or ""),
            norm(row.get("Description") or ""),
        )
        for row in rows
    )
    occurrences: Counter = Counter()
    plan: list[dict[str, str]] = []
    for row in rows:
        base_key = (
            row.get("Date") or "",
            amount_key(row.get("Amount") or ""),
            norm(row.get("Merchant") or ""),
            norm(row.get("Description") or ""),
        )
        occurrences[base_key] += 1
        if str(row.get("Property") or "").strip():
            continue
        if excluded_reason(row):
            continue
        reason = personal_reason(row)
        if reason:
            prop, prop_reason = "Personal", f"explicit_personal_{reason}"
        else:
            prop, prop_reason = infer_property(row, indexes)
        category, category_reason = infer_category(row, bool(reason))
        target_type = CATEGORY_TYPES.get(category, "Operating Expenses")
        needs_evidence = (
            prop == GENERIC_PROPERTY
            or category_reason in {"generic_expense_fallback", "generic_revenue_fallback"}
            or category_reason == "semantic_mortgage"
        )
        plan.append(
            {
                "fingerprint": fingerprint(row, occurrences[base_key]),
                "occurrence": str(occurrences[base_key]),
                "source_key_cardinality": str(cardinalities[base_key]),
                "date": row.get("Date") or "",
                "merchant": row.get("Merchant") or "",
                "description": row.get("Description") or "",
                "amount": amount_key(row.get("Amount") or ""),
                "current_type": row.get("Type") or "",
                "current_category": row.get("Category") or "",
                "target_property": prop,
                "target_property_id": GENERIC_PROPERTY_ID if prop == GENERIC_PROPERTY else "",
                "property_reason": prop_reason,
                "target_category": category,
                "category_reason": category_reason,
                "target_type": target_type,
                "evidence_status": "provider_or_receipt_required" if needs_evidence else "rule_supported",
            }
        )
    return plan


def latest_export() -> Path:
    candidates = list(REPORTS.glob("baselane_export_all_transactions.*.csv"))
    if not candidates:
        raise SystemExit("No full Baselane transaction export found")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def write_outputs(source: Path, plan: list[dict[str, str]], prefix: Path) -> None:
    counts = Counter(item["target_property"] for item in plan)
    category_counts = Counter(item["target_category"] for item in plan)
    evidence_count = sum(item["evidence_status"] != "rule_supported" for item in plan)
    payload = {
        "status": "review" if evidence_count else "ok",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": str(source),
        "policy": {
            "personal_exceptions": sorted(PERSONAL_PATTERNS),
            "default_property": GENERIC_PROPERTY,
            "default_property_policy_name": "ECO Systems LLC generic business fallback",
            "default_property_id": GENERIC_PROPERTY_ID,
            "excluded_properties": sorted(EXCLUDED_PROPERTIES),
            "excluded_text_rules": sorted(EXCLUDED_TEXT_PATTERNS),
            "mutation_contract": "Exact-match live row by fingerprint fields and occurrence; then mutate and read back.",
        },
        "unassigned_source_count": len(plan),
        "planned_property_coverage_count": len(plan),
        "evidence_required_count": evidence_count,
        "property_counts": dict(counts.most_common()),
        "category_counts": dict(category_counts.most_common()),
        "rows": plan,
    }
    prefix.parent.mkdir(parents=True, exist_ok=True)
    prefix.with_suffix(".json").write_text(json.dumps(payload, indent=2) + "\n")
    with prefix.with_suffix(".csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(plan[0]) if plan else ["fingerprint"])
        writer.writeheader()
        writer.writerows(plan)
    lines = [
        "# Baselane Full Property Coverage Plan",
        "",
        f"- Source: `{source}`",
        f"- Blank-property source rows: **{len(plan)}**",
        f"- Rows with planned property and category: **{len(plan)}**",
        f"- Provider or receipt review: **{evidence_count}**",
        f"- Generic ECO business fallback (`{GENERIC_PROPERTY}`): **{counts[GENERIC_PROPERTY]}**",
        f"- Explicit personal exceptions: **{counts['Personal']}**",
        "- Coolwood is excluded from this workspace.",
        "- This artifact is a plan, not proof of a live mutation.",
        "",
        "## Property Targets",
        "",
    ]
    lines.extend(f"- {name}: {count}" for name, count in counts.most_common())
    lines.extend(["", "## Category Targets", ""])
    lines.extend(f"- {name}: {count}" for name, count in category_counts.most_common())
    prefix.with_suffix(".md").write_text("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=None)
    parser.add_argument(
        "--output-prefix",
        type=Path,
        default=DROPBOX_REPORTS / "baselane_full_property_coverage_plan",
    )
    args = parser.parse_args()
    source = (args.source or latest_export()).resolve()
    with source.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    plan = build_plan(rows)
    write_outputs(source, plan, args.output_prefix.resolve())
    print(
        json.dumps(
            {
                "source": str(source),
                "unassigned": len(plan),
                "planned": len(plan),
                "output": str(args.output_prefix.resolve()),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
