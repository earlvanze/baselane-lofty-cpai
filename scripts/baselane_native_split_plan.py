#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


ROOT = Path(__file__).absolute().parents[1]
DEFAULT_RULES_PATH = ROOT / "config" / "baselane_native_split_rules.json"
DEFAULT_REAL_ESTATE_BASE = Path(
    os.environ.get("BASELANE_NATIVE_SPLIT_REAL_ESTATE_BASE")
    or os.environ.get("REAL_ESTATE_BASE")
    or "/mnt/c/Users/digit/Dropbox/Real Estate"
)
NO_DAO_MORTGAGE_RULE = "no_dao_mortgage_statement_split"
NO_DAO_UNASSIGNED_SCOPE = "unassigned_no_dao_mortgage"
NO_DAO_UNASSIGNED_PROPERTY = "UNASSIGNED_COOWNER_MORTGAGE_RESPONSIBILITY"

MADISON_SPLIT_PROPERTIES = [
    ("84 Madison Ave", 4),
    ("86 Madison Ave", 5),
    ("88 Madison Ave", 6),
    ("90 Madison Ave", 5),
]
MADISON_EQUAL_PROPERTIES = [
    ("84 Madison Ave", 1),
    ("86 Madison Ave", 1),
    ("88 Madison Ave", 1),
    ("90 Madison Ave", 1),
]
CATEGORY_BY_RULE = {
    "madison_morgan_linen_4_5_6_5": "Cleaning & Maintenance",
    "madison_spectrum_6958_equal": "Phone, Cable & Internet",
    "madison_spectrum_equal": "Phone, Cable & Internet",
    "madison_netflix_equal": "Phone, Cable & Internet",
    "madison_hulu_equal": "Phone, Cable & Internet",
    "madison_county_waste_equal": "Garbage & Recycling",
    "hospitable_april_2026_listing_weights": "Software Subscriptions",
    "pricelabs_april_2026_listing_weights": "Software Subscriptions",
    NO_DAO_MORTGAGE_RULE: "Mortgage Payments",
}

sys.path.insert(0, str(ROOT / "scripts"))
import split_ledger_public_financials as public_split  # noqa: E402
from baselane_ecogl_data_quality_autonomy import NO_DAO_MORTGAGE_PROPERTY_KEYS  # noqa: E402


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def norm(value: object) -> str:
    return " ".join(str(value or "").strip().lower().split())


def amount_decimal(value: object) -> Decimal:
    return Decimal(str(value or "0").replace(",", "").replace("$", "").strip() or "0")


def parse_iso_date(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def today_utc() -> date:
    override = os.environ.get("BASELANE_NATIVE_SPLIT_TODAY")
    parsed = parse_iso_date(override)
    return parsed or datetime.now(timezone.utc).date()


def row_in_date_window(row: dict[str, str], lookback_days: int | None) -> bool:
    if lookback_days is None:
        return True
    row_date = parse_iso_date(row.get("ISODate"))
    if row_date is None:
        return False
    return today_utc() - timedelta(days=lookback_days) <= row_date <= today_utc()


def cents(value: Decimal) -> int:
    return int((value * Decimal("100")).to_integral_value(rounding=ROUND_HALF_UP))


def from_cents(value: int) -> str:
    return str((Decimal(value) / Decimal("100")).quantize(Decimal("0.01")))


def allocate_amount(amount: Decimal, weights: list[int]) -> list[str]:
    total_weight = sum(weights)
    amount_cents = cents(amount)
    raw = [Decimal(amount_cents) * Decimal(weight) / Decimal(total_weight) for weight in weights]
    allocated = [int(part.to_integral_value(rounding=ROUND_HALF_UP)) for part in raw]
    diff = amount_cents - sum(allocated)
    if diff:
        allocated[-1] += diff
    return [from_cents(part) for part in allocated]


def read_csv(path: Path) -> tuple[list[dict[str, str]], list[str], list[str]]:
    if not path.is_file():
        return [], [], [f"missing_csv:{path}"]
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            return [{key: str(value or "") for key, value in row.items()} for row in reader], list(reader.fieldnames or []), []
    except Exception as exc:  # noqa: BLE001
        return [], [], [f"unreadable_csv:{path}:{exc}"]


def stable_id(row: dict[str, str], rule: str) -> str:
    material = {
        "rule": rule,
        "baselane_id": row.get("BaselaneId"),
        "date": row.get("ISODate") or row.get("Date"),
        "amount": row.get("Amount"),
        "merchant": row.get("Merchant"),
        "description": row.get("Description"),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def property_ids(rows: list[dict[str, str]]) -> dict[str, str]:
    ids: dict[str, str] = {}
    for row in rows:
        prop = row.get("Property", "").strip()
        prop_id = row.get("PropertyId", "").strip()
        if prop and prop_id and prop not in ids:
            ids[prop] = prop_id
    return ids


def category_tag_ids(rows: list[dict[str, str]]) -> dict[str, str]:
    ids: dict[str, str] = {}
    for row in rows:
        category = row.get("Category", "").strip()
        tag_id = row.get("TagId", "").strip()
        if category and tag_id and category not in ids:
            ids[category] = tag_id
    for row in rows:
        if norm(row.get("Merchant")) == "spectrum" and row.get("TagId", "").strip():
            ids.setdefault("Phone, Cable & Internet", row["TagId"].strip())
            ids.setdefault("Utilities", row["TagId"].strip())
    return ids


def source_rows_by_property(rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        prop = row.get("Property", "").strip()
        if prop:
            grouped.setdefault(prop, []).append(row)
    return grouped


def no_dao_property_key(row: dict[str, str]) -> str | None:
    haystack = public_split.normalize(f"{row.get('Property', '')} {row.get('Account', '')}")
    for key in NO_DAO_MORTGAGE_PROPERTY_KEYS:
        if public_split.normalize(key) in haystack:
            return key
    return None


def is_no_dao_mortgage_parent(row: dict[str, str]) -> bool:
    return (
        no_dao_property_key(row) is not None
        and public_split.is_unsplit_citadel_mortgage_parent(row)
        and amount_decimal(row.get("Amount")) < 0
        and bool(row.get("BaselaneId"))
    )


def resolve_property_root(property_name: str, roots: list[str], real_estate_base: Path) -> tuple[str | None, str, float]:
    if not real_estate_base.is_dir():
        return None, "", 0.0
    root_path, score, rel = public_split.best_match(property_name, roots, str(real_estate_base))
    if not root_path:
        return None, rel, score
    return root_path, rel, score


def mortgage_split_id(row: dict[str, str]) -> str:
    return stable_id(row, NO_DAO_MORTGAGE_RULE)


def amount_string(value: float) -> str:
    return public_split.format_amount(round(float(value), 2))


def split_component(
    *,
    row: dict[str, str],
    property_name: str,
    property_id: str | None,
    amount: float,
    category: str,
    tag_id: str,
    merchant_name: str,
    property_scope: str = "property",
) -> dict[str, Any]:
    return {
        "property": property_name,
        "property_id": property_id,
        "property_scope": property_scope,
        "property_id_required": property_scope != NO_DAO_UNASSIGNED_SCOPE,
        "amount": amount_string(amount),
        "category": category,
        "tag_id": tag_id,
        "merchant_name": merchant_name,
        "source_property": row.get("Property"),
    }


def blocked_mortgage_record(row: dict[str, str], reason: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": mortgage_split_id(row),
        "rule": NO_DAO_MORTGAGE_RULE,
        "status": "blocked_missing_mortgage_split_evidence",
        "issues": [reason],
        "baselane_id": row.get("BaselaneId"),
        "date": row.get("Date"),
        "iso_date": row.get("ISODate"),
        "account": row.get("Account"),
        "merchant": row.get("Merchant"),
        "description": row.get("Description"),
        "amount": row.get("Amount"),
        "source_property": row.get("Property"),
        "category": "Mortgage Payments",
        "tag_id": row.get("TagId"),
        "split_count": 0,
        "missing_properties": [],
        "missing_tag": False,
        "mutation_mode": "plan_only",
        "splits": [],
        **extra,
    }


def build_no_dao_mortgage_record(
    row: dict[str, str],
    *,
    rows_by_property: dict[str, list[dict[str, str]]],
    prop_ids: dict[str, str],
    tag_ids: dict[str, str],
    roots: list[str],
    real_estate_base: Path,
) -> dict[str, Any]:
    property_name = row.get("Property", "").strip()
    ym = public_split.row_year_month(row)
    if not property_name or not ym:
        return blocked_mortgage_record(row, "missing_property_or_statement_month")
    root_path, rel, score = resolve_property_root(property_name, roots, real_estate_base)
    if not root_path:
        return blocked_mortgage_record(row, "missing_property_root", best_match_root=rel, best_match_score=round(score, 3))
    statement = public_split.mortgage_statement_for_root(
        root_path,
        ym[0],
        ym[1],
        float(amount_decimal(row.get("Amount"))),
        property_name,
    )
    if not statement:
        return blocked_mortgage_record(row, "missing_matching_mortgage_statement", best_match_root=rel, best_match_score=round(score, 3))
    paid_total = statement.get("paid_total")
    if paid_total is None or abs(abs(float(amount_decimal(row.get("Amount")))) - float(paid_total)) > 1.0:
        return blocked_mortgage_record(
            row,
            "statement_total_does_not_match_parent_amount",
            statement_path=statement.get("statement_path"),
            statement_component_source=statement.get("statement_component_source"),
        )

    required_tags = {
        "Mortgage Payments": tag_ids.get("Mortgage Payments", row.get("TagId", "")),
        "Mortgage Principal Payments": tag_ids.get("Mortgage Principal Payments", ""),
        "Mortgage Interest Payments": tag_ids.get("Mortgage Interest Payments", ""),
        "Insurance": tag_ids.get("Insurance", ""),
        "Taxes": tag_ids.get("Taxes", ""),
    }
    property_id = prop_ids.get(property_name, "")
    missing_properties = [] if property_id else [property_name]

    insurance, taxes = public_split.citadel_escrow_components(
        rows_by_property.get(property_name, []),
        ym[0],
        ym[1],
        float(statement["paid_escrow"]),
    )
    escrow_schedule = public_split.escrow_native_split_schedule(
        year=ym[0],
        month=ym[1],
        insurance=insurance,
        taxes=taxes,
        statement_path=statement.get("statement_path"),
    )
    paid_fees = float(statement.get("paid_fees") or 0.0)
    required_split_categories = ["Mortgage Principal Payments", "Mortgage Interest Payments"]
    if paid_fees > 0:
        required_split_categories.append("Mortgage Payments")
    if insurance > 0:
        required_split_categories.append("Insurance")
    if taxes > 0:
        required_split_categories.append("Taxes")
    missing_tags = [category for category in required_split_categories if not required_tags.get(category)]
    splits = [
        split_component(
            row=row,
            property_name=NO_DAO_UNASSIGNED_PROPERTY,
            property_id=None,
            property_scope=NO_DAO_UNASSIGNED_SCOPE,
            amount=-float(statement["paid_principal"]),
            category="Mortgage Principal Payments",
            tag_id=required_tags["Mortgage Principal Payments"],
            merchant_name=f"{property_name} Mortgage Principal - co-owner responsibility",
        ),
        split_component(
            row=row,
            property_name=NO_DAO_UNASSIGNED_PROPERTY,
            property_id=None,
            property_scope=NO_DAO_UNASSIGNED_SCOPE,
            amount=-float(statement["paid_interest"]),
            category="Mortgage Interest Payments",
            tag_id=required_tags["Mortgage Interest Payments"],
            merchant_name=f"{property_name} Mortgage Interest - co-owner responsibility",
        ),
    ]
    if paid_fees > 0:
        splits.append(
            split_component(
                row=row,
                property_name=NO_DAO_UNASSIGNED_PROPERTY,
                property_id=None,
                property_scope=NO_DAO_UNASSIGNED_SCOPE,
                amount=-paid_fees,
                category="Mortgage Payments",
                tag_id=required_tags["Mortgage Payments"],
                merchant_name=f"{property_name} Mortgage Fees - co-owner responsibility",
            )
        )
    if insurance > 0:
        splits.append(
            split_component(
                row=row,
                property_name=property_name,
                property_id=property_id,
                amount=-insurance,
                category="Insurance",
                tag_id=required_tags["Insurance"],
                merchant_name=f"{property_name} Mortgage Escrow - Insurance",
            )
        )
    if taxes > 0:
        splits.append(
            split_component(
                row=row,
                property_name=property_name,
                property_id=property_id,
                amount=-taxes,
                category="Taxes",
                tag_id=required_tags["Taxes"],
                merchant_name=f"{property_name} Mortgage Escrow - Property Taxes",
            )
        )
    escrow_issues = []
    if escrow_schedule["native_split_update_required"] and not escrow_schedule["native_split_update_ready"]:
        escrow_issues.append("escrow_native_split_requires_statement_evidence")
    issues = (
        [f"missing_tag:{category}" for category in missing_tags]
        + [f"missing_property_id:{prop}" for prop in missing_properties]
        + escrow_issues
    )
    if escrow_issues:
        status = "blocked_missing_mortgage_split_evidence"
    elif missing_tags or missing_properties:
        status = "blocked_missing_split_metadata"
    else:
        status = "ready_native_split"
    return {
        "id": mortgage_split_id(row),
        "rule": NO_DAO_MORTGAGE_RULE,
        "status": status,
        "issues": issues,
        "baselane_id": row.get("BaselaneId"),
        "date": row.get("Date"),
        "iso_date": row.get("ISODate"),
        "account": row.get("Account"),
        "merchant": row.get("Merchant"),
        "description": row.get("Description"),
        "amount": row.get("Amount"),
        "source_property": property_name,
        "category": "Mortgage Payments",
        "tag_id": row.get("TagId"),
        "split_count": len(splits),
        "missing_properties": missing_properties,
        "missing_tag": bool(missing_tags),
        "mutation_mode": "plan_only",
        "splits": splits,
        "statement_path": statement.get("statement_path"),
        "statement_component_source": statement.get("statement_component_source"),
        "escrow_native_split_schedule": escrow_schedule,
        "escrow_native_split_schedule_status": escrow_schedule["status"],
        "escrow_native_split_schedule_months": escrow_schedule["schedule_months"],
        "escrow_statement_evidence_required": escrow_schedule["statement_evidence_required"],
        "escrow_statement_evidence_present": escrow_schedule["statement_evidence_present"],
        "escrow_insurance_monthly_amount": escrow_schedule["insurance_monthly_amount"],
        "escrow_taxes_monthly_amount": escrow_schedule["taxes_monthly_amount"],
        "escrow_insurance_native_split_amount": escrow_schedule["insurance_native_split_amount"],
        "escrow_taxes_native_split_amount": escrow_schedule["taxes_native_split_amount"],
        "escrow_native_split_amount_source": escrow_schedule["native_split_amount_source"],
        "escrow_annual_reset": escrow_schedule["annual_escrow_reset"],
        "escrow_annual_reset_reason": escrow_schedule["annual_escrow_reset_reason"],
        "escrow_schedule_effective_start_month": escrow_schedule["effective_start_month"],
        "escrow_schedule_effective_end_month": escrow_schedule["effective_end_month"],
        "property_root": root_path,
        "best_match_root": rel,
        "best_match_score": round(score, 3),
        "policy": (
            "principal and interest intentionally unassigned; escrow tax and insurance remain property-scoped "
            "operating expenses; statement-backed escrow disbursement amounts refresh live Baselane native "
            "split amounts for the next 12 months"
        ),
    }


def build_no_dao_mortgage_records(
    rows: list[dict[str, str]],
    *,
    prop_ids: dict[str, str],
    tag_ids: dict[str, str],
    real_estate_base: Path,
    lookback_days: int | None,
) -> list[dict[str, Any]]:
    if not real_estate_base.is_dir():
        return []
    roots = public_split.build_property_roots(str(real_estate_base))
    rows_by_property = source_rows_by_property(rows)
    return [
        build_no_dao_mortgage_record(
            row,
            rows_by_property=rows_by_property,
            prop_ids=prop_ids,
            tag_ids=tag_ids,
            roots=roots,
            real_estate_base=real_estate_base,
        )
        for row in rows
        if row_in_date_window(row, lookback_days) and is_no_dao_mortgage_parent(row)
    ]


def load_rules(path: Path = DEFAULT_RULES_PATH) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    rules = data.get("rules") if isinstance(data, dict) else None
    return [rule for rule in rules or [] if isinstance(rule, dict)]


def configured_split_rule(row: dict[str, str], rules: list[dict[str, Any]]) -> tuple[str, list[tuple[str, int]], str] | None:
    merchant = row.get("Merchant", "")
    description = row.get("Description", "")
    notes = row.get("Notes", "")
    combined = f"{merchant} {description} {notes}".lower()
    if " | " in merchant or " - " in merchant or "#" in merchant:
        return None
    for rule in rules:
        match = rule.get("match") if isinstance(rule.get("match"), dict) else {}
        source_property = str(match.get("source_property") or "")
        if source_property and row.get("Property") != source_property:
            continue
        contains_any = [str(value).lower() for value in match.get("contains_any") or []]
        if contains_any and not any(value in combined for value in contains_any):
            continue
        targets = []
        for target in rule.get("targets") or []:
            if not isinstance(target, dict):
                continue
            property_name = str(target.get("property") or "").strip()
            weight = int(target.get("weight") or 0)
            if property_name and weight > 0:
                targets.append((property_name, weight))
        if targets:
            return str(rule.get("id") or ""), targets, str(rule.get("category") or CATEGORY_BY_RULE.get(str(rule.get("id") or ""), ""))
    return None


def split_rule(row: dict[str, str], rules: list[dict[str, Any]] | None = None) -> tuple[str, list[tuple[str, int]], str] | None:
    configured = configured_split_rule(row, rules if rules is not None else load_rules())
    if configured:
        return configured
    merchant = row.get("Merchant", "")
    description = row.get("Description", "")
    notes = row.get("Notes", "")
    combined = f"{merchant} {description} {notes}".lower()
    if " | " in merchant or " - " in merchant or "#" in merchant:
        return None
    if "morgan linen services" in combined and row.get("Property") == "88 Madison Ave":
        return "madison_morgan_linen_4_5_6_5", MADISON_SPLIT_PROPERTIES, CATEGORY_BY_RULE["madison_morgan_linen_4_5_6_5"]
    is_spectrum_madison_parent = "spectrum" in combined
    is_spectrum_madison_marker = (
        "spectrum" in combined
        and "*split*" in combined
        and "25%" in combined
        and "84-90 madison" in combined
    )
    if (is_spectrum_madison_parent or is_spectrum_madison_marker) and row.get("Property") == "90 Madison Ave":
        return "madison_spectrum_6958_equal", MADISON_EQUAL_PROPERTIES, CATEGORY_BY_RULE["madison_spectrum_6958_equal"]
    return None


def build_record(row: dict[str, str], prop_ids: dict[str, str], tag_ids: dict[str, str], rules: list[dict[str, Any]]) -> dict[str, Any]:
    rule, targets, configured_category = split_rule(row, rules) or ("", [], "")
    category = configured_category or CATEGORY_BY_RULE.get(rule, "")
    allocations = allocate_amount(amount_decimal(row.get("Amount")), [weight for _prop, weight in targets])
    splits = []
    missing_properties = []
    for (property_name, weight), split_amount in zip(targets, allocations, strict=True):
        property_id = prop_ids.get(property_name, "")
        if not property_id:
            missing_properties.append(property_name)
        splits.append(
            {
                "property": property_name,
                "property_id": property_id,
                "amount": split_amount,
                "weight": weight,
                "category": category,
                "tag_id": tag_ids.get(category, ""),
            }
        )
    missing_tag = not tag_ids.get(category)
    status = "ready_native_split" if splits and not missing_properties and not missing_tag else "blocked_missing_split_metadata"
    return {
        "id": stable_id(row, rule),
        "rule": rule,
        "status": status,
        "baselane_id": row.get("BaselaneId"),
        "date": row.get("Date"),
        "iso_date": row.get("ISODate"),
        "account": row.get("Account"),
        "merchant": row.get("Merchant"),
        "description": row.get("Description"),
        "amount": row.get("Amount"),
        "source_property": row.get("Property"),
        "category": category,
        "tag_id": tag_ids.get(category, ""),
        "split_count": len(splits),
        "missing_properties": missing_properties,
        "missing_tag": missing_tag,
        "mutation_mode": "plan_only",
        "splits": splits,
    }


def escrow_native_split_update_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in records:
        schedule = record.get("escrow_native_split_schedule")
        if not isinstance(schedule, dict) or not schedule.get("native_split_update_required"):
            continue
        monthly_splits = schedule.get("monthly_native_splits")
        rows.append(
            {
                "id": record.get("id"),
                "property": record.get("source_property"),
                "record_status": record.get("status"),
                "issues": record.get("issues") or [],
                "statement_path": record.get("statement_path") or schedule.get("source_statement_path"),
                "statement_component_source": record.get("statement_component_source"),
                "schedule_status": schedule.get("status"),
                "native_split_update_ready": bool(schedule.get("native_split_update_ready")),
                "upstream": schedule.get("upstream"),
                "native_split_update_mode": schedule.get("native_split_update_mode"),
                "effective_start_month": schedule.get("effective_start_month"),
                "effective_end_month": schedule.get("effective_end_month"),
                "schedule_months": schedule.get("schedule_months"),
                "insurance_monthly_amount": schedule.get("insurance_monthly_amount"),
                "taxes_monthly_amount": schedule.get("taxes_monthly_amount"),
                "insurance_native_split_amount": schedule.get("insurance_native_split_amount"),
                "taxes_native_split_amount": schedule.get("taxes_native_split_amount"),
                "escrow_disbursement_amount": schedule.get("escrow_disbursement_amount"),
                "native_split_amount_source": schedule.get("native_split_amount_source"),
                "annual_escrow_reset": schedule.get("annual_escrow_reset"),
                "annual_escrow_reset_reason": schedule.get("annual_escrow_reset_reason"),
                "native_split_schedule_semantics": schedule.get("native_split_schedule_semantics"),
                "monthly_native_splits": monthly_splits if isinstance(monthly_splits, list) else [],
            }
        )
    return rows


def escrow_native_split_update_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    updates = escrow_native_split_update_rows(records)
    handled_statuses = {"applied", "already_applied"}
    return {
        "escrow_native_split_update_count": len(updates),
        "escrow_native_split_update_ready_count": sum(
            1
            for item in updates
            if item.get("native_split_update_ready") and item.get("record_status") == "ready_native_split"
        ),
        "escrow_native_split_update_handled_count": sum(
            1 for item in updates if item.get("record_status") in handled_statuses
        ),
        "escrow_native_split_update_blocked_count": sum(
            1
            for item in updates
            if not item.get("native_split_update_ready")
            or (
                item.get("record_status") != "ready_native_split"
                and item.get("record_status") not in handled_statuses
            )
        ),
        "escrow_native_split_update_properties": sorted(
            {str(item.get("property")) for item in updates if item.get("property")}
        ),
        "escrow_native_split_updates": updates,
    }


def build_report(source_index: Path, lookback_days: int | None = None, real_estate_base: Path = DEFAULT_REAL_ESTATE_BASE) -> dict[str, Any]:
    rows, fields, errors = read_csv(source_index)
    rules = load_rules()
    prop_ids = property_ids(rows)
    tag_ids = category_tag_ids(rows)
    vendor_records = [
        build_record(row, prop_ids, tag_ids, rules)
        for row in rows
        if row_in_date_window(row, lookback_days) and split_rule(row, rules) and amount_decimal(row.get("Amount")) < 0
    ]
    mortgage_records = build_no_dao_mortgage_records(
        rows,
        prop_ids=prop_ids,
        tag_ids=tag_ids,
        real_estate_base=Path(real_estate_base),
        lookback_days=lookback_days,
    )
    records = vendor_records + mortgage_records
    status_counts = Counter(record["status"] for record in records)
    rule_counts = Counter(record["rule"] for record in records)
    blocked_count = sum(1 for record in records if not str(record["status"]).startswith("ready_"))
    escrow_update_summary = escrow_native_split_update_summary(records)
    digest = hashlib.sha256(json.dumps(records, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return {
        "generated_at": iso_z(),
        "status": "ok" if records and not errors and not blocked_count else "review",
        "policy": "Plan only; Baselane native split mutation requires a separate guarded apply path and explicit approval gate.",
        "mutation_mode": "plan_only",
        "rules_path": str(DEFAULT_RULES_PATH),
        "real_estate_base": str(real_estate_base),
        "lookback_days": lookback_days,
        "source_index": str(source_index),
        "source_field_count": len(fields),
        "source_errors": errors,
        "row_count": len(records),
        "vendor_row_count": len(vendor_records),
        "no_dao_mortgage_row_count": len(mortgage_records),
        "ready_native_split_count": status_counts.get("ready_native_split", 0),
        "blocked_count": blocked_count,
        **escrow_update_summary,
        "rule_counts": dict(sorted(rule_counts.items())),
        "status_counts": dict(sorted(status_counts.items())),
        "idempotency_digest": digest,
        "records": records,
    }


def native_split_digest(record: dict[str, Any]) -> str:
    material = {
        "rule": record.get("rule"),
        "baselane_id": record.get("baselane_id"),
        "amount": record.get("amount"),
        "escrow_native_split_schedule": record.get("escrow_native_split_schedule"),
        "splits": [
            {
                "amount": split.get("amount"),
                "property_id": split.get("property_id"),
                "tag_id": split.get("tag_id"),
                "property": split.get("property"),
                "category": split.get("category"),
            }
            for split in record.get("splits") or []
        ],
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()


def read_json(path: Path | None) -> Any:
    if not path or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def reconciled_apply_status(action: dict[str, Any]) -> str:
    status = str(action.get("status") or "")
    execution = action.get("execution") if isinstance(action.get("execution"), dict) else {}
    if status == "already_applied":
        return "already_applied"
    if status == "ready" and execution.get("return_code") == 0:
        return "applied"
    return ""


def reconcile_with_apply_report(report: dict[str, Any], apply_report_path: Path | None) -> dict[str, Any]:
    apply_report = read_json(apply_report_path)
    if not isinstance(apply_report, dict):
        return report
    actions_by_id = {
        str(action.get("id") or ""): action
        for action in apply_report.get("actions") or []
        if isinstance(action, dict) and action.get("id")
    }
    reconciled_records = []
    reconciled_counts: Counter[str] = Counter()
    for record in report.get("records") or []:
        if not isinstance(record, dict):
            continue
        updated = dict(record)
        action = actions_by_id.get(str(record.get("id") or ""))
        action_status = reconciled_apply_status(action) if isinstance(action, dict) else ""
        if action_status and action.get("split_digest") == native_split_digest(record):
            updated["planned_status"] = record.get("status")
            updated["status"] = action_status
            updated["apply_status"] = action_status
            updated["apply_report"] = str(apply_report_path)
            updated["split_digest"] = action.get("split_digest")
            reconciled_counts[action_status] += 1
        reconciled_records.append(updated)
    if not reconciled_counts:
        return report
    status_counts = Counter(record["status"] for record in reconciled_records)
    ready_count = status_counts.get("ready_native_split", 0)
    blocked_count = sum(
        1
        for record in reconciled_records
        if not str(record["status"]).startswith("ready_") and record["status"] not in {"already_applied", "applied"}
    )
    digest = hashlib.sha256(json.dumps(reconciled_records, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    report.update(
        {
            "status": "ok" if reconciled_records and not report.get("source_errors") and not ready_count and not blocked_count else report["status"],
            "mutation_mode": "plan_reconciled",
            "ready_native_split_count": ready_count,
            "handled_native_split_count": reconciled_counts.get("already_applied", 0) + reconciled_counts.get("applied", 0),
            "already_applied_count": reconciled_counts.get("already_applied", 0),
            "applied_count": reconciled_counts.get("applied", 0),
            "blocked_count": blocked_count,
            "status_counts": dict(sorted(status_counts.items())),
            "idempotency_digest": digest,
            "apply_report": str(apply_report_path),
            "records": reconciled_records,
            **escrow_native_split_update_summary(reconciled_records),
        }
    )
    return report


def write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    fields = [
        "id",
        "rule",
        "status",
        "baselane_id",
        "iso_date",
        "merchant",
        "amount",
        "source_property",
        "category",
        "tag_id",
        "split_count",
        "planned_status",
        "apply_status",
        "escrow_native_split_schedule_status",
        "escrow_native_split_schedule_months",
        "escrow_schedule_effective_start_month",
        "escrow_schedule_effective_end_month",
        "escrow_insurance_monthly_amount",
        "escrow_taxes_monthly_amount",
        "escrow_insurance_native_split_amount",
        "escrow_taxes_native_split_amount",
        "escrow_native_split_amount_source",
        "escrow_annual_reset_reason",
        "escrow_statement_evidence_present",
        "splits",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in records:
            row = dict(record)
            row["splits"] = json.dumps(record.get("splits") or [], sort_keys=True)
            writer.writerow({field: row.get(field, "") for field in fields})


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Baselane Native Split Plan",
        "",
        f"- Status: `{report['status']}`",
        f"- Mutation mode: `{report['mutation_mode']}`",
        f"- Ready native splits: `{report['ready_native_split_count']}`",
        f"- Handled native splits: `{report.get('handled_native_split_count', 0)}`",
        f"- Blocked: `{report['blocked_count']}`",
        f"- Digest: `{report['idempotency_digest']}`",
        "",
        "## Records",
        "",
    ]
    for record in report.get("records") or []:
        lines.append(
            f"- `{record.get('rule')}` — Baselane `{record.get('baselane_id')}` — "
            f"{record.get('iso_date')} — {record.get('merchant')} — {record.get('amount')} — `{record.get('status')}`"
        )
    if not report.get("records"):
        lines.append("- None")
    lines.append("")
    escrow_updates = report.get("escrow_native_split_updates") or []
    lines.extend(
        [
            "## Escrow Native Split Updates",
            "",
            f"- Statement-backed escrow updates: `{report.get('escrow_native_split_update_count', 0)}`",
            f"- Ready: `{report.get('escrow_native_split_update_ready_count', 0)}`",
            f"- Handled: `{report.get('escrow_native_split_update_handled_count', 0)}`",
            f"- Blocked: `{report.get('escrow_native_split_update_blocked_count', 0)}`",
            "",
        ]
    )
    for update in escrow_updates:
        lines.append(
            f"- {update.get('property')} - `{update.get('schedule_status')}` - "
            f"{update.get('effective_start_month')} through {update.get('effective_end_month')} - "
            f"Insurance `{update.get('insurance_native_split_amount') or update.get('insurance_monthly_amount')}` / "
            f"Taxes `{update.get('taxes_native_split_amount') or update.get('taxes_monthly_amount')}` - "
            f"statement escrow `{update.get('escrow_disbursement_amount')}` - `{update.get('native_split_update_mode')}`"
        )
    if not escrow_updates:
        lines.append("- None")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a non-mutating Baselane native split plan for deterministic shared vendors.")
    parser.add_argument("--source-index", type=Path, default=Path(__file__).absolute().parents[1] / "reports" / "baselane_source_transaction_index.csv")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--apply-report", type=Path)
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=int(os.environ.get("BASELANE_NATIVE_SPLIT_LOOKBACK_DAYS", "90")),
        help="Only plan candidate transactions this many days back from today. Use --no-date-window to disable.",
    )
    parser.add_argument("--no-date-window", action="store_true")
    args = parser.parse_args()

    root = Path(__file__).absolute().parents[1]
    report = build_report(args.source_index, None if args.no_date_window else args.lookback_days)
    report = reconcile_with_apply_report(report, args.apply_report)
    report_path = args.report or root / "reports" / "baselane_native_split_plan.json"
    csv_path = args.csv or root / "reports" / "baselane_native_split_plan.csv"
    markdown_path = args.markdown or root / "reports" / "baselane_native_split_plan.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_csv(csv_path, report["records"])
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ["status", "row_count", "ready_native_split_count", "blocked_count", "idempotency_digest"]}, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
