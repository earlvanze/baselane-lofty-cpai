#!/usr/bin/env python3
"""Plan deterministic replacements for misused Non-Property Expense tags."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from baselane_full_property_coverage_plan import (
    CATEGORY_TYPES,
    DROPBOX_REPORTS,
    GENERIC_PROPERTY,
    GENERIC_PROPERTY_ID,
    REPORTS,
    amount_key,
    fingerprint,
    norm,
    row_text,
    semantic_category,
)


UNRESOLVED_CATEGORIES = {
    "",
    "NON PROPERTY EXPENSE",
    "UNCATEGORIZED",
    "UNCATEGORIZED EXPENSE",
}
PERSONAL_EXCEPTIONS = {
    "groceries": re.compile(
        r"\b(ALDI|CARREF\w*|CONSUM|GIANT EAGLE|GROCERY|KROGER|LIDL|"
        r"MERCADONA|SAFEWAY|SAINSBURY\w*|SUPERMARKET|TRADER JOE|WHOLE FOODS)\b",
        re.I,
    ),
    "pharmacy": re.compile(
        r"\b(APOTHEK\w*|CVS|FARMAC\w*|FCIA|MEDICALITY|PHARM\w*|RITE AID|WALGREENS)\b",
        re.I,
    ),
    "remitly": re.compile(r"\b(REMITLY|RMTLY)\b", re.I),
    "bunq": re.compile(r"\bBUNQ\b", re.I),
}
SEMANTIC_CATEGORIES = [
    (
        "check_payment_fee",
        re.compile(r"\bFEE FOR CHECK PAYMENT\b", re.I),
        "Bank Fees",
    ),
    (
        "hemlane_property_expense",
        re.compile(r"\bHEML\b.*\b(PURCHASE|RAZ)\b", re.I),
        "Repairs",
    ),
    (
        "retailer_property_supply",
        re.compile(
            r"\b(GOODWILL|SAMS CLUB|TARGET DEBIT|WALMART(?:\.COM)?|WAL-MART|WM SUPERCENTER)\b",
            re.I,
        ),
        "Supplies",
    ),
    (
        "jpmc_utility_fee",
        re.compile(r"\bPSVJ\b.*\bJPMC FEE\b", re.I),
        "Water & Sewer",
    ),
    (
        "cash_app_property_contractor",
        re.compile(r"\bCASH APP\*", re.I),
        "Other Operating Expenses",
    ),
    (
        "cash_transfer",
        re.compile(r"\b(VENMO\s*\|\s*CASHOUT|TRANSFER_[OI])\b", re.I),
        "Transfers Between Accounts",
    ),
    (
        "software_or_digital_service",
        re.compile(r"\b(GAMESEAL|STEAM GAMES)\b", re.I),
        "Software Subscriptions",
    ),
    (
        "meals",
        re.compile(r"\b(BURGER KING|HELADERIA|TAPAS|YU ASIA)\b", re.I),
        "Meals & Food",
    ),
]


def personal_exception(row: dict[str, str]) -> str | None:
    text = row_text(row)
    for reason, pattern in PERSONAL_EXCEPTIONS.items():
        if pattern.search(text):
            return reason
    return None


def sign(row: dict[str, str]) -> str:
    return "in" if Decimal(amount_key(row.get("Amount") or "0")) >= 0 else "out"


def historical_indexes(
    rows: list[dict[str, str]],
) -> dict[str, dict[tuple[str, ...], Counter[str]]]:
    indexes: dict[str, dict[tuple[str, ...], Counter[str]]] = {
        "description": defaultdict(Counter),
        "property_description": defaultdict(Counter),
        "property_merchant": defaultdict(Counter),
        "merchant": defaultdict(Counter),
    }
    for row in rows:
        category = str(row.get("Category") or "").strip()
        if norm(category) in UNRESOLVED_CATEGORIES:
            continue
        description = norm(row.get("Description") or "")
        merchant = norm(row.get("Merchant") or "")
        prop = norm(row.get("Property") or "")
        direction = sign(row)
        if description:
            indexes["description"][(description, direction)][category] += 1
        if prop and description:
            indexes["property_description"][(prop, description, direction)][category] += 1
        if prop and merchant:
            indexes["property_merchant"][(prop, merchant, direction)][category] += 1
        if merchant:
            indexes["merchant"][(merchant, direction)][category] += 1
    return indexes


def unique(counter: Counter[str], minimum: int) -> tuple[str, int] | None:
    if len(counter) != 1:
        return None
    category, count = counter.most_common(1)[0]
    return (category, count) if count >= minimum else None


def dominant(counter: Counter[str], minimum: int = 3) -> tuple[str, int] | None:
    total = sum(counter.values())
    if total < minimum:
        return None
    category, count = counter.most_common(1)[0]
    return (category, count) if count / total >= 0.8 else None


def infer_category(
    row: dict[str, str],
    indexes: dict[str, dict[tuple[str, ...], Counter[str]]],
) -> tuple[str, str, int]:
    text = row_text(row)
    for reason, pattern, category in SEMANTIC_CATEGORIES:
        if pattern.search(text):
            return category, f"semantic_{reason}", 1

    description = norm(row.get("Description") or "")
    merchant = norm(row.get("Merchant") or "")
    prop = norm(row.get("Property") or "")
    direction = sign(row)
    tiers = [
        ("property_description", (prop, description, direction), 1),
        ("description", (description, direction), 1),
        ("property_merchant", (prop, merchant, direction), 1),
        ("merchant", (merchant, direction), 1),
    ]
    for tier, key, minimum in tiers:
        if all(key):
            match = unique(indexes[tier].get(key, Counter()), minimum)
            if match:
                return match[0], f"historical_unique_{tier}", match[1]
    for tier, key in (
        ("property_merchant", (prop, merchant, direction)),
        ("merchant", (merchant, direction)),
    ):
        if all(key):
            match = dominant(indexes[tier].get(key, Counter()))
            if match:
                return match[0], f"historical_dominant_{tier}", match[1]

    semantic = semantic_category(row)
    if semantic:
        return semantic[0], f"semantic_existing_{semantic[1]}", 1
    return (
        "Fees & Other Revenue"
        if Decimal(amount_key(row.get("Amount") or "0")) >= 0
        else "Other Operating Expenses",
        "generic_business_fallback",
        0,
    )


def build_plan(rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], Counter[str]]:
    indexes = historical_indexes(rows)
    cardinalities: Counter[tuple[str, str, str, str]] = Counter()
    for row in rows:
        cardinalities[
            (
                row.get("Date") or "",
                amount_key(row.get("Amount") or ""),
                norm(row.get("Merchant") or ""),
                norm(row.get("Description") or ""),
            )
        ] += 1

    occurrences: Counter[tuple[str, str, str, str]] = Counter()
    exceptions: Counter[str] = Counter()
    plan: list[dict[str, str]] = []
    for row in rows:
        base_key = (
            row.get("Date") or "",
            amount_key(row.get("Amount") or ""),
            norm(row.get("Merchant") or ""),
            norm(row.get("Description") or ""),
        )
        occurrences[base_key] += 1
        if norm(row.get("Category") or "") != "NON PROPERTY EXPENSE":
            continue
        exception = personal_exception(row)

        current_property = str(row.get("Property") or "").strip()
        target_property = (
            GENERIC_PROPERTY if current_property in {"", "Personal"} else current_property
        )
        category, reason, support = infer_category(row, indexes)
        # Equivalent categorized rows take precedence over a personal merchant
        # exception. Keep the exception only when history is not conclusive.
        if exception and not reason.startswith("historical_"):
            exceptions[exception] += 1
            continue
        plan.append(
            {
                "fingerprint": fingerprint(row, occurrences[base_key]),
                "occurrence": str(occurrences[base_key]),
                "source_key_cardinality": str(cardinalities[base_key]),
                "date": row.get("Date") or "",
                "merchant": row.get("Merchant") or "",
                "description": row.get("Description") or "",
                "amount": amount_key(row.get("Amount") or ""),
                "current_property": current_property,
                "current_category": row.get("Category") or "",
                "target_property": target_property,
                "target_property_id": (
                    GENERIC_PROPERTY_ID if target_property == GENERIC_PROPERTY else ""
                ),
                "target_category": category,
                "target_type": CATEGORY_TYPES.get(category, "Operating Expenses"),
                "category_reason": reason,
                "historical_support_count": str(support),
                "evidence_status": (
                    "fallback_reviewable"
                    if reason == "generic_business_fallback"
                    else "rule_supported"
                ),
            }
        )
    return plan, exceptions


def latest_export() -> Path:
    candidates = list(REPORTS.glob("baselane_export_all_transactions.*.csv"))
    if not candidates:
        raise SystemExit("No full Baselane transaction export found")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def write_outputs(
    source: Path,
    plan: list[dict[str, str]],
    exceptions: Counter[str],
    prefix: Path,
) -> None:
    reasons = Counter(row["category_reason"] for row in plan)
    categories = Counter(row["target_category"] for row in plan)
    payload = {
        "status": "ok",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": str(source),
        "policy": {
            "target_current_category": "Non-Property Expense",
            "personal_exceptions": sorted(PERSONAL_EXCEPTIONS),
            "business_property_fallback": GENERIC_PROPERTY,
            "mutation_contract": (
                "Exact source fingerprint/cardinality/occurrence, current Non-Property "
                "tag, metadata uniqueness, guarded mutation, and full live readback."
            ),
        },
        "planned_count": len(plan),
        "personal_exception_count": sum(exceptions.values()),
        "personal_exception_counts": dict(exceptions),
        "target_category_counts": dict(categories.most_common()),
        "reason_counts": dict(reasons.most_common()),
        "rows": plan,
    }
    prefix.parent.mkdir(parents=True, exist_ok=True)
    prefix.with_suffix(".json").write_text(json.dumps(payload, indent=2) + "\n")
    with prefix.with_suffix(".csv").open("w", newline="", encoding="utf-8") as handle:
        fields = list(plan[0]) if plan else ["fingerprint"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(plan)
    lines = [
        "# Baselane Non-Property Category Normalization Plan",
        "",
        f"- Source: `{source}`",
        f"- Planned business corrections: **{len(plan)}**",
        f"- Preserved explicit personal exceptions: **{sum(exceptions.values())}**",
        "- No mutation is represented until the apply report confirms full readback.",
        "",
        "## Categories",
        "",
    ]
    lines.extend(f"- {name}: {count}" for name, count in categories.most_common())
    lines.extend(["", "## Evidence", ""])
    lines.extend(f"- {name}: {count}" for name, count in reasons.most_common())
    prefix.with_suffix(".md").write_text("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument(
        "--output-prefix",
        type=Path,
        default=DROPBOX_REPORTS / "baselane_nonproperty_category_plan",
    )
    args = parser.parse_args()
    source = (args.source or latest_export()).resolve()
    with source.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    plan, exceptions = build_plan(rows)
    write_outputs(source, plan, exceptions, args.output_prefix.resolve())
    print(
        json.dumps(
            {
                "source": str(source),
                "planned": len(plan),
                "personal_exceptions": sum(exceptions.values()),
                "output": str(args.output_prefix.resolve()),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
