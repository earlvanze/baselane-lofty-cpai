#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import baselane_cf_untagged_rule_candidates as rule_candidates


FIELDS_REQUIRED = ["Date", "Amount", "Property", "Merchant", "Description", "Category"]
ACTION_FIELDS = [
    "id",
    "row_number",
    "property",
    "date",
    "amount",
    "merchant",
    "description",
    "old_category",
    "new_category",
    "match_value",
    "reason",
]
SAFE_CONFIDENCE = "high"
SAFE_MATCH_TYPE = "known_pattern"
HISTORICAL_DESCRIPTION_MIN_SUPPORT = 3
HISTORICAL_COUNTERPARTY_MIN_SUPPORT = 2
SAFE_HISTORICAL_CATEGORIES = {
    "Advertising",
    "Auto & Travel",
    "Cleaning & Maintenance",
    "Fees & Other Revenue",
    "Insurance",
    "Legal & Other Professional Fees",
    "Management Fees",
    "Mortgage Interest Payments",
    "Mortgage Payments",
    "Other Operating Expenses",
    "Rents",
    "Repairs",
    "Supplies",
    "Taxes",
    "Utilities",
}


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stable_digest(payload: object) -> str:
    material = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def normalize(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def normalize_counterparty(value: object) -> str:
    text = str(value or "").lower()
    text = re.split(r"\s+\|\s+|,", text, maxsplit=1)[0]
    text = re.sub(r"\btransfer[_ -]?o(?:ut)?\b", "transfer out", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def amount_sign(value: object) -> int:
    text = str(value or "0").replace(",", "").strip()
    if text.startswith("(") and text.endswith(")"):
        text = f"-{text[1:-1]}"
    try:
        amount = float(text)
    except ValueError:
        return 0
    if amount > 0:
        return 1
    if amount < 0:
        return -1
    return 0


def row_identity(row: dict[str, Any], match_value: str, category: str) -> str:
    return stable_digest(
        {
            "date": normalize(row.get("Date")),
            "property": normalize(row.get("Property")),
            "amount": normalize(row.get("Amount")),
            "merchant": normalize(row.get("Merchant")),
            "description": normalize(row.get("Description")),
            "match_value": match_value,
            "category": category,
        }
    )[:16]


def category_is_blank(value: object) -> bool:
    text = normalize(value)
    return not text or text.lower() in {"uncategorized", "uncategorized expense", "uncategorized income"}


def row_month(value: object) -> str | None:
    text = normalize(value)
    if not text:
        return None
    for fmt in ("%B %d, %Y", "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            parsed = datetime.strptime(text, fmt)
            return f"{parsed.year:04d}-{parsed.month:02d}"
        except ValueError:
            continue
    return None


def build_historical_category_evidence(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    description_counts: dict[str, Counter[str]] = {}
    for row in rows:
        description = normalize(row.get("Description"))
        category = normalize(row.get("Category"))
        if not description or not category or category_is_blank(category):
            continue
        description_counts.setdefault(description, Counter())[category] += 1

    evidence: dict[str, dict[str, Any]] = {}
    for description, counts in description_counts.items():
        if len(counts) != 1:
            continue
        category, support = counts.most_common(1)[0]
        if support < HISTORICAL_DESCRIPTION_MIN_SUPPORT or category not in SAFE_HISTORICAL_CATEGORIES:
            continue
        evidence[description] = {
            "category": category,
            "support": support,
            "match_value": f"Historical Exact Description: {description[:120]}",
            "reason": f"Exact description has {support} categorized historical row(s), all mapped to {category}.",
        }
    return evidence


def build_historical_counterparty_evidence(rows: list[dict[str, Any]]) -> dict[tuple[str, str, int], dict[str, Any]]:
    category_counts: dict[tuple[str, str, int], Counter[str]] = {}
    for row in rows:
        property_name = normalize(row.get("Property"))
        counterparty = normalize_counterparty(row.get("Merchant") or row.get("Description"))
        sign = amount_sign(row.get("Amount"))
        if not property_name or not counterparty or not sign:
            continue
        row_categories: set[str] = set()
        category = normalize(row.get("Category"))
        if category and not category_is_blank(category):
            row_categories.add(category)
        known = rule_candidates.infer_known_rule(row)
        if known and known[3] == SAFE_CONFIDENCE:
            known_category = normalize(known[2])
            if known_category and known_category in SAFE_HISTORICAL_CATEGORIES:
                row_categories.add(known_category)
        for row_category in row_categories:
            category_counts.setdefault((property_name, counterparty, sign), Counter())[row_category] += 1

    evidence: dict[tuple[str, str, int], dict[str, Any]] = {}
    for key, counts in category_counts.items():
        if len(counts) != 1:
            continue
        category, support = counts.most_common(1)[0]
        if support < HISTORICAL_COUNTERPARTY_MIN_SUPPORT or category not in SAFE_HISTORICAL_CATEGORIES:
            continue
        property_name, counterparty, sign = key
        direction = "expense" if sign < 0 else "income"
        evidence[key] = {
            "category": category,
            "support": support,
            "match_value": f"Historical Property Counterparty {direction}: {property_name} | {counterparty[:96]}",
            "reason": (
                f"Property/counterparty/{direction} has {support} categorized historical row(s), "
                f"all mapped to {category}, with no conflicting categories."
            ),
        }
    return evidence


def classify_historical_row(row: dict[str, Any], historical_evidence: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    description = normalize(row.get("Description"))
    if not description:
        return None
    evidence = historical_evidence.get(description)
    if not evidence:
        return None
    category = str(evidence["category"])
    match_value = str(evidence["match_value"])
    candidate_id = rule_candidates.rule_id("historical_exact_description", description, category, category)
    return {
        "rule_candidate_id": candidate_id,
        "match_value": match_value,
        "new_category": category,
        "target_cf_category": category,
        "reason": evidence["reason"],
    }


def classify_historical_counterparty_row(
    row: dict[str, Any],
    historical_evidence: dict[tuple[str, str, int], dict[str, Any]],
) -> dict[str, Any] | None:
    key = (
        normalize(row.get("Property")),
        normalize_counterparty(row.get("Merchant") or row.get("Description")),
        amount_sign(row.get("Amount")),
    )
    evidence = historical_evidence.get(key)
    if not evidence:
        return None
    category = str(evidence["category"])
    match_value = str(evidence["match_value"])
    candidate_id = rule_candidates.rule_id("historical_property_counterparty", "|".join(map(str, key)), category, category)
    return {
        "rule_candidate_id": candidate_id,
        "match_value": match_value,
        "new_category": category,
        "target_cf_category": category,
        "reason": evidence["reason"],
    }


def classify_safe_row(
    row: dict[str, Any],
    historical_description_evidence: dict[str, dict[str, Any]],
    historical_counterparty_evidence: dict[tuple[str, str, int], dict[str, Any]],
    historical_apply_month: str | None,
) -> dict[str, Any] | None:
    if not category_is_blank(row.get("Category")):
        return None
    known = rule_candidates.infer_known_rule(row)
    if known:
        match_value, cf_category, baselane_category, confidence, note = known
        if confidence == SAFE_CONFIDENCE:
            candidate_id = rule_candidates.rule_id(SAFE_MATCH_TYPE, match_value, cf_category, baselane_category)
            return {
                "rule_candidate_id": candidate_id,
                "match_value": match_value,
                "new_category": baselane_category,
                "target_cf_category": cf_category,
                "reason": note,
            }
    if historical_apply_month and row_month(row.get("Date")) == historical_apply_month:
        return classify_historical_row(row, historical_description_evidence) or classify_historical_counterparty_row(row, historical_counterparty_evidence)
    return None


def read_rows(path: Path) -> tuple[list[str], list[dict[str, Any]]]:
    with path.open("r", encoding="utf-8", errors="ignore", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        missing = [field for field in FIELDS_REQUIRED if field not in fieldnames]
        if missing:
            raise ValueError(f"Ledger missing required fields: {', '.join(missing)}")
        return fieldnames, list(reader)


def build_report(ledger: Path, out_ledger: Path, apply: bool, historical_apply_month: str | None = None) -> dict[str, Any]:
    fieldnames, rows = read_rows(ledger)
    output_rows = [dict(row) for row in rows]
    historical_description_evidence = build_historical_category_evidence(rows)
    historical_counterparty_evidence = build_historical_counterparty_evidence(rows)
    actions: list[dict[str, Any]] = []
    already_clean_count = 0

    for index, row in enumerate(output_rows, start=2):
        classification = classify_safe_row(row, historical_description_evidence, historical_counterparty_evidence, historical_apply_month)
        if not classification:
            known = rule_candidates.infer_known_rule(row)
            if known and normalize(row.get("Category")) == normalize(known[2]):
                already_clean_count += 1
            continue
        old_category = row.get("Category") or ""
        new_category = classification["new_category"]
        action = {
            "id": row_identity(row, classification["match_value"], new_category),
            "row_number": index,
            "property": row.get("Property"),
            "date": row.get("Date"),
            "amount": row.get("Amount"),
            "merchant": row.get("Merchant"),
            "description": row.get("Description"),
            "old_category": old_category,
            "new_category": new_category,
            "match_value": classification["match_value"],
            "reason": classification["reason"],
        }
        actions.append(action)
        row["Category"] = new_category

    category_counts = Counter(action["new_category"] for action in actions)
    rule_counts = Counter(action["match_value"] for action in actions)
    input_digest = stable_digest({"fieldnames": fieldnames, "rows": rows})
    output_digest = stable_digest({"fieldnames": fieldnames, "rows": output_rows})
    actions_digest = stable_digest({"actions": actions})
    status = "ok"
    if not rows:
        status = "review"
    report = {
        "status": status,
        "generated_at": iso_z(),
        "mode": "apply" if apply else "dry_run",
        "ledger": str(ledger),
        "out_ledger": str(out_ledger),
        "input_row_count": len(rows),
        "output_row_count": len(output_rows),
        "safe_action_count": len(actions),
        "already_clean_safe_pattern_count": already_clean_count,
        "historical_exact_description_rule_count": len(historical_description_evidence),
        "historical_property_counterparty_rule_count": len(historical_counterparty_evidence),
        "historical_exact_description_apply_month": historical_apply_month,
        "category_counts": dict(sorted(category_counts.items())),
        "rule_counts": dict(sorted(rule_counts.items())),
        "input_digest": input_digest,
        "output_digest": output_digest,
        "actions_digest": actions_digest,
        "idempotent": input_digest == output_digest if not actions else False,
        "output_written": False,
        "actions": actions,
    }
    if apply:
        out_ledger.parent.mkdir(parents=True, exist_ok=True)
        with out_ledger.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(output_rows)
        report["output_written"] = True
    return report


def write_actions_csv(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ACTION_FIELDS)
        writer.writeheader()
        for action in report["actions"]:
            writer.writerow({field: action.get(field, "") for field in ACTION_FIELDS})


def write_markdown(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# Baselane ECO GL Safe Category Apply",
        "",
        f"- Status: `{report['status']}`",
        f"- Mode: `{report['mode']}`",
        f"- Ledger: `{report['ledger']}`",
        f"- Output ledger: `{report['out_ledger']}`",
        f"- Output written: `{report['output_written']}`",
        f"- Safe category updates: `{report['safe_action_count']}`",
        f"- Already clean safe-pattern rows: `{report['already_clean_safe_pattern_count']}`",
        f"- Actions digest: `{report['actions_digest']}`",
        f"- Output digest: `{report['output_digest']}`",
        "",
        "## Rule Counts",
        "",
    ]
    if report["rule_counts"]:
        lines.extend(f"- `{name}`: `{count}`" for name, count in report["rule_counts"].items())
    else:
        lines.append("- No safe category updates needed.")
    lines.extend(["", "## Category Counts", ""])
    if report["category_counts"]:
        lines.extend(f"- `{name}`: `{count}`" for name, count in report["category_counts"].items())
    else:
        lines.append("- No category changes.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply deterministic high-confidence ECO GL category fixes to a derived reporting ledger.")
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--out-ledger", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--actions-csv", required=True, type=Path)
    parser.add_argument("--markdown", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--historical-apply-month", help="YYYY-MM month eligible for strict historical exact-description category fixes.")
    args = parser.parse_args()

    report = build_report(args.ledger, args.out_ledger, args.apply, args.historical_apply_month)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_actions_csv(report, args.actions_csv)
    write_markdown(report, args.markdown)
    print(json.dumps({key: report[key] for key in ("status", "mode", "safe_action_count", "output_written", "actions_digest")}, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
