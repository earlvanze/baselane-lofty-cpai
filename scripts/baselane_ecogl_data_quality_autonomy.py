#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import re
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import baselane_cf_untagged_rule_candidates as rule_candidates
from coownership_mortgage_policy import (
    NO_DAO_MORTGAGE_STATES,
    NO_DAO_MORTGAGE_PROPERTY_KEYS,
    is_no_dao_mortgage_property_or_state,
)


SAFE_CONFIDENCE = "high"
SAFE_MATCH_TYPE = "known_pattern"
NO_DAO_MORTGAGE_GUARD_MIN_YEAR = 1900
PENDING_UNASSIGNED_MATERIAL_DEBIT_MINIMUM = 100.0
SOURCE_FUTURE_DATE_GRACE_DAYS = 0
RAW_LEDGER_CANDIDATES = (
    "baselane_weekly_clean_reporting_ledger.csv",
    "baselane_weekly_safe_category_reporting_ledger.csv",
    "baselane_weekly_deduped_reporting_ledger.csv",
)
ALLOWED_OPERATING_ESCROW_CATEGORIES = {
    "city state local taxes",
    "flood",
    "insurance",
    "rental dwelling",
    "taxes",
}
SAFE_ACTION_FIELDS = [
    "id",
    "weekly_queue_id",
    "rule_candidate_id",
    "match_value",
    "target_baselane_category",
    "target_cf_category",
    "property",
    "date",
    "amount",
    "merchant",
    "description",
    "reason",
]
EXCEPTION_FIELDS = [
    "id",
    "queue_type",
    "status",
    "property",
    "date",
    "amount",
    "merchant",
    "description",
    "reason",
    "suggested_baselane_category",
    "suggested_cf_category",
    "current_baselane_type",
    "current_baselane_category",
    "source_csv",
    "source_line",
]
SOURCE_INDEX_NAME = "baselane_source_transaction_index.csv"
KNOWN_PROPERTY_PAYMENT_SPLITS_CONFIG = "config/baselane_known_property_payment_splits.json"
HEMLANE_NET_PM_PROPERTY_KEYS = {
    "1278 e 187th st",
    "1456 w 85th st",
    "25 circle dr",
    "428 cross st",
    "5541 s peoria st",
    "566 nash st",
    "7542 7656 s colfax ave",
    "8143 s sangamon st",
    "917 pawnee ave",
}
PROPERTY_TOKEN_ALIASES = {
    "ave": "avenue",
    "blvd": "boulevard",
    "cir": "circle",
    "dr": "drive",
    "ln": "lane",
    "pl": "place",
    "rd": "road",
    "st": "street",
}


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "missing", "path": str(path)}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return data


def stable_digest(payload: dict[str, Any]) -> str:
    return rule_candidates.stable_digest(payload)


def weekly_queue_id(prefix: str, row: dict[str, Any]) -> str:
    existing = str(row.get("id") or "").strip()
    if existing:
        return existing
    return f"{prefix}-{stable_digest(row)[:12]}"


def safe_action_id(row: dict[str, Any], match_value: str, baselane_category: str) -> str:
    material = {
        "date": row.get("Date"),
        "property": row.get("Property"),
        "amount": row.get("Amount"),
        "merchant": row.get("Merchant"),
        "description": row.get("Description"),
        "match_value": match_value,
        "baselane_category": baselane_category,
    }
    return stable_digest(material)[:16]


def auto_safe_rule_ids(candidate_packet: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for record in candidate_packet.get("records") or []:
        if not isinstance(record, dict):
            continue
        if record.get("confidence") != SAFE_CONFIDENCE or record.get("match_type") != SAFE_MATCH_TYPE:
            continue
        record_id = str(record.get("id") or "").strip()
        if record_id:
            ids.add(record_id)
    return ids


def classify_untagged_row(row: dict[str, Any], safe_rule_ids: set[str]) -> dict[str, Any]:
    known = rule_candidates.infer_known_rule(row)
    if not known:
        return {"safe": False, "reason": str(row.get("review_reason") or "no deterministic rule matched")}
    match_value, cf_category, baselane_category, confidence, note = known
    candidate_id = rule_candidates.rule_id(SAFE_MATCH_TYPE, match_value, cf_category, baselane_category)
    if confidence != SAFE_CONFIDENCE or candidate_id not in safe_rule_ids:
        return {
            "safe": False,
            "reason": f"{match_value} is {confidence or 'unknown'} confidence; keep as exception.",
            "match_value": match_value,
            "rule_candidate_id": candidate_id,
        }
    return {
        "safe": True,
        "reason": note,
        "match_value": match_value,
        "target_cf_category": cf_category,
        "target_baselane_category": baselane_category,
        "rule_candidate_id": candidate_id,
    }


def text(value: Any) -> str:
    return "" if value is None else str(value)


def normalize(value: Any) -> str:
    return " ".join(
        "".join(ch.lower() if ch.isalnum() else " " for ch in text(value)).split()
    )


def parse_year(value: Any) -> int | None:
    raw = text(value).strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).year
        except ValueError:
            continue
    return None


def parse_date_key(value: Any) -> str:
    raw = text(value).strip()
    if not raw:
        return ""
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return normalize(raw)


def parse_source_date(value: Any) -> date | None:
    raw = text(value).strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def parse_amount_key(value: Any) -> str:
    raw = text(value).strip().replace("$", "").replace(",", "")
    if not raw:
        return ""
    if raw.startswith("(") and raw.endswith(")"):
        raw = f"-{raw[1:-1]}"
    try:
        return f"{float(raw):.2f}"
    except ValueError:
        return normalize(value)


def amount_cents(value: Any) -> int | None:
    normalized = parse_amount_key(value)
    try:
        return round(float(normalized) * 100)
    except ValueError:
        return None


def source_index_rows(root: Path) -> tuple[dict[tuple[str, str, str, str], list[dict[str, Any]]], dict[str, Any]]:
    path = root / "reports" / SOURCE_INDEX_NAME
    metadata: dict[str, Any] = {
        "source_index_path": str(path),
        "source_index_status": "missing",
        "source_index_mtime": "",
        "source_index_row_count": 0,
    }
    buckets: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    if not path.is_file():
        return buckets, metadata
    metadata["source_index_status"] = "ok"
    metadata["source_index_mtime"] = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                metadata["source_index_row_count"] += 1
                if not text(row.get("Category")).strip() or not text(row.get("TagId")).strip():
                    continue
                property_key = normalize(row.get("Property"))
                date_key = parse_date_key(row.get("ISODate") or row.get("Date"))
                amount_key = parse_amount_key(row.get("Amount"))
                merchant_key = normalize(row.get("Merchant"))
                if not property_key or not date_key or not amount_key:
                    continue
                key = (property_key, date_key, amount_key, merchant_key)
                buckets.setdefault(key, []).append(row)
    except (OSError, csv.Error) as exc:
        metadata["source_index_status"] = "unreadable"
        metadata["source_index_error"] = str(exc)
    return buckets, metadata


def source_index_matches(row: dict[str, Any], buckets: dict[tuple[str, str, str, str], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    property_key = normalize(row.get("Property"))
    date_key = parse_date_key(row.get("ISODate") or row.get("Date"))
    amount_key = parse_amount_key(row.get("Amount"))
    merchant_key = normalize(row.get("Merchant"))
    if not property_key or not date_key or not amount_key:
        return []
    exact = buckets.get((property_key, date_key, amount_key, merchant_key), [])
    if exact:
        return exact
    matches: list[dict[str, Any]] = []
    for (candidate_property, candidate_date, candidate_amount, _), candidates in buckets.items():
        if (candidate_property, candidate_date, candidate_amount) == (property_key, date_key, amount_key):
            matches.extend(candidates)
    return matches


def is_management_fee_label(label: Any) -> bool:
    normalized = text(label).strip().lower()
    return "management fee" in normalized or "pm fee" in normalized


def is_hemlane_net_pm_property(property_name: Any) -> bool:
    property_key = canonical_property_key(property_name)
    return any(
        canonical_property_key(key) in property_key
        for key in HEMLANE_NET_PM_PROPERTY_KEYS
    )


def canonical_property_key(value: Any) -> str:
    """Normalize address aliases without treating city/state suffixes as identity."""
    tokens = [PROPERTY_TOKEN_ALIASES.get(token, token) for token in normalize(value).split()]
    return " ".join(token for token in tokens if token != "and")


def property_alias_matches(left: Any, right: Any) -> bool:
    left_key = canonical_property_key(left)
    right_key = canonical_property_key(right)
    return bool(left_key and right_key and (left_key in right_key or right_key in left_key))


def parse_amount(value: Any) -> float | None:
    try:
        return float(text(value).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


def approved_pm_evidence(root: Path, item: dict[str, Any], month: str) -> dict[str, Any] | None:
    """Find exact approved PM accrual/direct-split evidence in the active ledger.

    The raw Baselane transaction index intentionally excludes accounting overlay
    rows. Those rows are nevertheless authoritative when the ledger records a
    typed PM marker, the target month/property, and the exact CF amount.
    """
    login_export = read_json(root / "reports" / "baselane_login_export_report.json")
    ledger_path = Path(text(login_export.get("canonical_path")))
    if not ledger_path.is_file():
        return None
    target_amount = parse_amount(item.get("cf_value") or item.get("current_value"))
    if target_amount is None:
        return None
    try:
        with ledger_path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                notes = text(row.get("Notes"))
                if "|pm|" not in notes or f"|{month}|" not in notes:
                    continue
                if text(row.get("Category")).strip() != "Property Management":
                    continue
                if not property_alias_matches(item.get("property"), row.get("Property")):
                    continue
                row_amount = parse_amount(row.get("Amount"))
                if row_amount is not None and round(row_amount, 2) == round(target_amount, 2):
                    return {
                        "status": "approved_pm_accrual",
                        "ledger_path": str(ledger_path),
                        "date": row.get("Date"),
                        "merchant": row.get("Merchant"),
                        "amount": row.get("Amount"),
                    }
                legacy = re.search(r"Voided legacy manual PM row of \$(\d+(?:\.\d+)?)", notes)
                if legacy and round(float(legacy.group(1)), 2) == round(abs(target_amount), 2):
                    return {
                        "status": "hemlane_direct_split_pm_void",
                        "ledger_path": str(ledger_path),
                        "date": row.get("Date"),
                        "merchant": row.get("Merchant"),
                        "legacy_amount": legacy.group(1),
                    }
    except (OSError, csv.Error):
        return None
    return None


def is_source_quality_conflict(item: dict[str, Any]) -> bool:
    """Keep CF statement/template divergence out of ECO GL source-fix scope.

    PM-fee rows are retained because the known failure mode is duplicate source
    timing from a first-day accrual cron. IL/OH/TN Hemlane properties are an
    intentional exception: their rent deposits are already net of PM fees, so
    an empty Baselane PM accrual is correct and must not be "fixed" by adding a
    second fee. Other CF_has_value/GL_empty rows must be resolved by rebuilding
    the CF statements from raw property GL, not by writing Baselane source data
    from workbook values.
    """
    return (
        text(item.get("action")).strip() == "cf_has_value_gl_empty"
        and is_management_fee_label(item.get("label"))
        and not is_hemlane_net_pm_property(item.get("property"))
    )


def no_dao_mortgage_property_key(row: dict[str, Any]) -> str | None:
    haystack = normalize(f"{row.get('Property', '')} {row.get('Account', '')}")
    raw_haystack = f"{row.get('Property', '')} {row.get('Account', '')}"
    if not is_no_dao_mortgage_property_or_state(raw_haystack):
        return None
    for key in NO_DAO_MORTGAGE_PROPERTY_KEYS:
        if normalize(key) in haystack:
            return key
    for state in ("IL", "OH", "TN"):
        if re.search(rf"(?:^|[\s,\/\\]){state}(?:[\s,\/\\]|$)", raw_haystack, re.IGNORECASE):
            return f"state:{state.lower()}"
    return "state:no-dao-mortgage"


def raw_no_dao_mortgage_violation_reason(row: dict[str, Any]) -> str:
    if no_dao_mortgage_property_key(row) is None:
        return ""
    year = parse_year(row.get("Date"))
    if year is not None and year < NO_DAO_MORTGAGE_GUARD_MIN_YEAR:
        return ""

    category = normalize(row.get("Category"))
    subcategory = normalize(row.get("Sub-category"))
    row_type = normalize(row.get("Type"))
    searchable = normalize(
        " ".join(
            text(row.get(field))
            for field in ("Merchant", "Description", "Type", "Category", "Sub-category", "Notes")
        )
    )
    operating_escrow = (
        row_type == "operating expenses" and category in ALLOWED_OPERATING_ESCROW_CATEGORIES
    ) or (
        category == "operating expenses" and subcategory in ALLOWED_OPERATING_ESCROW_CATEGORIES
    )
    if operating_escrow:
        return ""
    # The clean reporting export flattens Baselane's transfer type to
    # "Transaction" while retaining the transfer sub-category.  Keep the
    # immutable transfer marker and the retained transfer classification as
    # the evidence, rather than requiring the original type/category shape.
    internal_transfer = "internal transfer" in searchable and (
        category == "transfers between accounts"
        or subcategory == "transfers between accounts"
    )
    if internal_transfer:
        return ""
    if "mortgage payments" in category or "mortgage payments" in subcategory or (
        "mortgage payment" in searchable and not operating_escrow
    ):
        return "No-DAO-mortgage property has a raw Baselane mortgage payment row."
    if "mortgage escrow" in searchable and not operating_escrow:
        return "No-DAO-mortgage property has a raw non-operating mortgage escrow row."
    if (
        "mortgage principal" in searchable
        or "mortgage interest" in searchable
        or "interest only" in searchable
        or "principal curtailment" in searchable
    ):
        return "No-DAO-mortgage property has raw principal/interest debt language."
    known_servicer = "loandepot" in searchable or "newrez" in searchable or "shellpoin" in searchable
    debt_type = row_type in {"loan payments capex", "debt service"} or "loan payment" in row_type
    debt_category = "loan payment" in category or "principal" in category or "interest" in category
    if known_servicer and (debt_type or debt_category):
        return "No-DAO-mortgage property has a raw loan-servicer debt row."
    return ""


def raw_ledger_candidate_paths(root: Path) -> list[Path]:
    reports = root / "reports"
    explicit = text(os.environ.get("BASELANE_ECOGL_RAW_LEDGER_PATHS")).strip()
    paths: list[Path] = []
    if explicit:
        paths.extend(Path(part).expanduser() for part in explicit.split(":") if part.strip())
    paths.extend(reports / name for name in RAW_LEDGER_CANDIDATES)
    return paths


def materialized_operating_escrow_split_keys(root: Path) -> set[tuple[str, str, str]]:
    """Identify split parents that Baselane's reporting CSV still renders as raw."""
    path = root / "reports" / SOURCE_INDEX_NAME
    if not path.is_file():
        return set()
    categories_by_parent: dict[tuple[str, str, str], set[str]] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            category = normalize(row.get("Category"))
            if category not in {"insurance", "taxes"}:
                continue
            merchant = normalize(row.get("Merchant"))
            if "mortgage escrow" not in merchant:
                continue
            key = (
                normalize(row.get("Property")),
                parse_date_key(row.get("ISODate") or row.get("Date")),
                normalize(row.get("Description")),
            )
            if all(key):
                categories_by_parent.setdefault(key, set()).add(category)
    return {key for key, categories in categories_by_parent.items() if {"insurance", "taxes"} <= categories}


def raw_no_dao_mortgage_exceptions(root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    exceptions: list[dict[str, Any]] = []
    scanned_paths: list[str] = []
    seen: set[str] = set()
    materialized_escrow_keys = materialized_operating_escrow_split_keys(root)
    for path in raw_ledger_candidate_paths(root):
        if not path.is_file():
            continue
        scanned_paths.append(str(path))
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for source_line, row in enumerate(csv.DictReader(handle), start=2):
                reason = raw_no_dao_mortgage_violation_reason(row)
                if not reason:
                    continue
                parent_key = (
                    normalize(row.get("Property")),
                    parse_date_key(row.get("Date")),
                    normalize(row.get("Description")),
                )
                if parent_key in materialized_escrow_keys:
                    continue
                dedupe_key = stable_digest(
                    {
                        "property": row.get("Property"),
                        "date": row.get("Date"),
                        "amount": row.get("Amount"),
                        "merchant": row.get("Merchant"),
                        "description": row.get("Description"),
                        "type": row.get("Type"),
                        "category": row.get("Category"),
                    }
                )
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                exceptions.append(
                    {
                        "id": f"no-dao-mortgage-source-{dedupe_key[:12]}",
                        "queue_type": "raw_no_dao_mortgage",
                        "status": "blocked_action",
                        "property": row.get("Property"),
                        "date": row.get("Date"),
                        "amount": row.get("Amount"),
                        "merchant": row.get("Merchant"),
                        "description": row.get("Description"),
                        "reason": reason,
                        "suggested_baselane_category": "",
                        "suggested_cf_category": "",
                        "current_baselane_type": row.get("Type"),
                        "current_baselane_category": row.get("Category"),
                        "source_csv": str(path),
                        "source_line": source_line,
                    }
                )
    return exceptions, scanned_paths


def pending_unassigned_material_source_exceptions(root: Path) -> list[dict[str, Any]]:
    """Block material pending property debits until their native split posts."""
    path = root / "reports" / SOURCE_INDEX_NAME
    if not path.is_file():
        return []
    exceptions: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for source_line, row in enumerate(csv.DictReader(handle), start=2):
                if normalize(row.get("Pending")) not in {"true", "1", "yes"}:
                    continue
                if text(row.get("PropertyId")).strip() or text(row.get("Property")).strip():
                    continue
                try:
                    amount = float(str(row.get("Amount") or "0").replace(",", ""))
                except ValueError:
                    continue
                if amount > -PENDING_UNASSIGNED_MATERIAL_DEBIT_MINIMUM:
                    continue
                category_text = normalize(
                    " ".join(text(row.get(field)) for field in ("Type", "Category", "Sub-category", "Merchant", "Description"))
                )
                property_affecting = any(token in category_text for token in (
                    "tax", "mortgage", "loan payment", "insurance", "rental dwelling", "property management",
                ))
                if not property_affecting:
                    continue
                dedupe_key = str(row.get("BaselaneId") or "").strip() or stable_digest(
                    {field: row.get(field) for field in ("Date", "Amount", "Merchant", "Description", "BankAccountId")}
                )
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                exceptions.append(
                    {
                        "id": f"pending-unassigned-source-{dedupe_key[:12]}",
                        "queue_type": "pending_unassigned_material_source_transaction",
                        "status": "blocked_action",
                        "property": "",
                        "date": row.get("ISODate") or row.get("Date"),
                        "amount": row.get("Amount"),
                        "merchant": row.get("Merchant"),
                        "description": row.get("Description"),
                        "reason": "Material pending unassigned property-affecting debit requires a posted native property/tag split.",
                        "suggested_baselane_category": "",
                        "suggested_cf_category": "",
                        "current_baselane_type": row.get("Type"),
                        "current_baselane_category": row.get("Category"),
                        "source_csv": str(path),
                        "source_line": source_line,
                    }
                )
    except (OSError, csv.Error):
        return []
    return exceptions


def known_property_payment_split_exceptions(root: Path) -> list[dict[str, Any]]:
    """Verify approved material payment allocations after Baselane replaces a parent with children."""
    config = read_json(root / KNOWN_PROPERTY_PAYMENT_SPLITS_CONFIG)
    payments = config.get("payments") if isinstance(config.get("payments"), list) else []
    path = root / "reports" / SOURCE_INDEX_NAME
    if not payments or not path.is_file():
        return []
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            source_rows = list(csv.DictReader(handle))
    except (OSError, csv.Error):
        return []

    exceptions: list[dict[str, Any]] = []
    for payment in payments:
        if not isinstance(payment, dict):
            continue
        payment_id = text(payment.get("id")).strip()
        parent_id = text(payment.get("parent_baselane_id")).strip()
        expected_total = amount_cents(payment.get("parent_amount"))
        expected_date = parse_date_key(payment.get("date"))
        expected_bank = text(payment.get("bank_account_id")).strip()
        merchant_contains = normalize(payment.get("merchant_contains"))
        required_components = payment.get("required_components") if isinstance(payment.get("required_components"), list) else []
        if not payment_id or not parent_id or expected_total is None or not expected_date or not required_components:
            continue

        parents = [row for row in source_rows if text(row.get("BaselaneId")).strip() == parent_id]
        if parents and normalize(parents[0].get("Pending")) in {"true", "1", "yes"}:
            # The generic pending gate owns this state and avoids duplicate exceptions.
            continue
        matching_rows = [
            row for row in source_rows
            if parse_date_key(row.get("ISODate") or row.get("Date")) == expected_date
            and text(row.get("BankAccountId")).strip() == expected_bank
            and merchant_contains in normalize(" ".join((text(row.get("Merchant")), text(row.get("Description")))))
        ]
        if parents:
            reason = "Posted known property payment parent remains unsplit; native property/tag children are required."
        elif not matching_rows:
            reason = "Known property payment parent is absent and no matching native property/tag children were found in the fresh source index."
        else:
            missing_components: list[str] = []
            for component in required_components:
                if not isinstance(component, dict):
                    continue
                expected_property = normalize(component.get("property"))
                expected_amount = amount_cents(component.get("amount"))
                expected_tag = text(component.get("tag_id")).strip()
                expected_category = normalize(component.get("category"))
                matched = any(
                    normalize(row.get("Property")) == expected_property
                    and amount_cents(row.get("Amount")) == expected_amount
                    and text(row.get("PropertyId")).strip()
                    and text(row.get("TagId")).strip() == expected_tag
                    and normalize(row.get("Category")) == expected_category
                    for row in matching_rows
                )
                if not matched:
                    missing_components.append(f"{component.get('property')} {component.get('amount')}")
            total_cents = sum(amount_cents(row.get("Amount")) or 0 for row in matching_rows)
            untagged_rows = [
                row for row in matching_rows
                if not text(row.get("PropertyId")).strip() or not text(row.get("TagId")).strip()
            ]
            if not missing_components and total_cents == expected_total and not untagged_rows:
                continue
            details: list[str] = []
            if missing_components:
                details.append("missing required parcel allocations: " + ", ".join(missing_components))
            if total_cents != expected_total:
                details.append(f"child total {total_cents / 100:.2f} does not equal settled debit {expected_total / 100:.2f}")
            if untagged_rows:
                details.append(f"{len(untagged_rows)} matching child row(s) lack a property or tag")
            reason = "Known property payment native split is incomplete: " + "; ".join(details) + "."
        exceptions.append(
            {
                "id": f"known-property-payment-split-{payment_id}",
                "queue_type": "known_property_payment_split",
                "status": "blocked_action",
                "property": "",
                "date": expected_date,
                "amount": f"{expected_total / 100:.2f}",
                "merchant": payment.get("merchant_contains"),
                "description": payment.get("description") or payment_id,
                "reason": reason,
                "suggested_baselane_category": "",
                "suggested_cf_category": "",
                "current_baselane_type": "",
                "current_baselane_category": "",
                "source_csv": str(path),
                "source_line": "",
            }
        )
    return exceptions


def future_dated_source_exceptions(root: Path, *, today: date | None = None) -> list[dict[str, Any]]:
    """Block reporting when source rows are dated after the current reporting day.

    Future manual journals belong in a forecast schedule, not a live Baselane
    ledger.  Including them in the current full-column cash basis would make
    transfer recommendations and investor reporting depend on obligations that
    have not yet occurred.
    """
    path = root / "reports" / SOURCE_INDEX_NAME
    if not path.is_file():
        return []
    cutoff = today or date.today()
    exceptions: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for source_line, row in enumerate(csv.DictReader(handle), start=2):
                transaction_date = parse_source_date(row.get("ISODate") or row.get("Date"))
                if transaction_date is None or transaction_date <= cutoff:
                    continue
                notes = str(row.get("Notes") or "").strip().upper()
                if (
                    transaction_date.year == cutoff.year
                    and transaction_date.month == cutoff.month
                    and notes.startswith("AOPS-")
                ):
                    continue
                amount = parse_amount_key(row.get("Amount"))
                if not amount or amount == "0.00":
                    continue
                dedupe_key = str(row.get("BaselaneId") or "").strip() or stable_digest(
                    {field: row.get(field) for field in ("Date", "Amount", "Merchant", "Description", "BankAccountId")}
                )
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                exceptions.append(
                    {
                        "id": f"future-dated-source-{dedupe_key[:12]}",
                        "queue_type": "future_dated_source_transaction",
                        "status": "blocked_action",
                        "property": row.get("Property"),
                        "date": transaction_date.isoformat(),
                        "amount": row.get("Amount"),
                        "merchant": row.get("Merchant"),
                        "description": row.get("Description"),
                        "reason": f"Source transaction is future dated after reporting day {cutoff.isoformat()}.",
                        "suggested_baselane_category": "",
                        "suggested_cf_category": "",
                        "current_baselane_type": row.get("Type"),
                        "current_baselane_category": row.get("Category"),
                        "source_csv": str(path),
                        "source_line": source_line,
                    }
                )
    except (OSError, csv.Error):
        return []
    return exceptions


def build_report(root: Path) -> dict[str, Any]:
    reports = root / "reports"
    weekly_cf = read_json(reports / "baselane_weekly_cf_statement_sync_report.json")
    conflict_plan = read_json(reports / "baselane_cf_conflict_resolution_plan.json")
    untagged_packet = read_json(reports / "baselane_cf_untagged_review_packet.json")
    candidate_packet = read_json(reports / "baselane_cf_untagged_rule_candidates.json")

    safe_rule_ids = auto_safe_rule_ids(candidate_packet)
    source_index, source_index_metadata = source_index_rows(root)
    safe_actions: list[dict[str, Any]] = []
    resolved_source_tagged_rows: list[dict[str, Any]] = []
    untagged_exceptions: list[dict[str, Any]] = []
    for row in untagged_packet.get("rows") or []:
        if not isinstance(row, dict) or row.get("review_required") is not True:
            continue
        source_matches = source_index_matches(row, source_index)
        if source_matches:
            resolved_source_tagged_rows.append(
                {
                    "id": weekly_queue_id("untagged", row),
                    "queue_type": "untagged",
                    "status": "resolved_source_tagged",
                    "property": row.get("Property"),
                    "date": row.get("Date"),
                    "amount": row.get("Amount"),
                    "merchant": row.get("Merchant"),
                    "description": row.get("Description"),
                    "current_baselane_type": source_matches[0].get("Type"),
                    "current_baselane_category": source_matches[0].get("Category"),
                    "source_csv": source_index_metadata["source_index_path"],
                    "source_line": "",
                    "baselane_ids": sorted({text(item.get("BaselaneId")) for item in source_matches if text(item.get("BaselaneId")).strip()}),
                    "tag_ids": sorted({text(item.get("TagId")) for item in source_matches if text(item.get("TagId")).strip()}),
                    "match_count": len(source_matches),
                    "reason": "Current source transaction index confirms a nonblank Baselane category and TagId.",
                }
            )
            continue
        classification = classify_untagged_row(row, safe_rule_ids)
        row_queue_id = weekly_queue_id("untagged", row)
        if classification.get("safe") is True:
            safe_actions.append(
                {
                    "id": safe_action_id(
                        row,
                        str(classification.get("match_value") or ""),
                        str(classification.get("target_baselane_category") or ""),
                    ),
                    "weekly_queue_id": row_queue_id,
                    "rule_candidate_id": classification.get("rule_candidate_id"),
                    "match_value": classification.get("match_value"),
                    "target_baselane_category": classification.get("target_baselane_category"),
                    "target_cf_category": classification.get("target_cf_category"),
                    "property": row.get("Property"),
                    "date": row.get("Date"),
                    "amount": row.get("Amount"),
                    "merchant": row.get("Merchant"),
                    "description": row.get("Description"),
                    "reason": classification.get("reason"),
                }
            )
        else:
            untagged_exceptions.append(
                {
                    "id": row_queue_id,
                    "queue_type": "untagged",
                    "status": "exception",
                    "property": row.get("Property"),
                    "date": row.get("Date"),
                    "amount": row.get("Amount"),
                    "merchant": row.get("Merchant"),
                    "description": row.get("Description"),
                    "reason": classification.get("reason"),
                    "suggested_baselane_category": row.get("suggested_baselane_category"),
                    "suggested_cf_category": row.get("suggested_cf_category"),
                }
            )

    conflict_results = [item for item in conflict_plan.get("results") or [] if isinstance(item, dict)]
    approved_pm_conflicts: list[dict[str, Any]] = []
    conflict_exceptions = []
    for item in conflict_results:
        if item.get("status") not in {"needs_approval", "blocked_action"} or not is_source_quality_conflict(item):
            continue
        evidence = approved_pm_evidence(root, item, text(conflict_plan.get("month")))
        if evidence:
            approved_pm_conflicts.append({"id": str(item.get("id") or weekly_queue_id("conflict", item)), "property": item.get("property"), "evidence": evidence})
            continue
        conflict_exceptions.append(
            {
            "id": str(item.get("id") or weekly_queue_id("conflict", item)),
            "queue_type": "conflict",
            "status": str(item.get("status") or "unknown"),
            "property": item.get("property"),
            "date": "",
            "amount": item.get("current_value"),
            "merchant": "",
            "description": item.get("label"),
            "reason": item.get("reason") or item.get("action"),
            "suggested_baselane_category": "",
            "suggested_cf_category": "",
            }
        )
    no_dao_mortgage_exceptions, no_dao_mortgage_scanned_paths = raw_no_dao_mortgage_exceptions(root)
    pending_unassigned_exceptions = pending_unassigned_material_source_exceptions(root)
    known_property_payment_exceptions = known_property_payment_split_exceptions(root)
    cutoff_value = os.environ.get("BASELANE_REPORTING_CUTOFF_DATE", "").strip()
    try:
        reporting_cutoff = date.fromisoformat(cutoff_value) if cutoff_value else None
    except ValueError as exc:
        raise ValueError(
            f"invalid BASELANE_REPORTING_CUTOFF_DATE={cutoff_value!r}; expected YYYY-MM-DD"
        ) from exc
    future_dated_exceptions = future_dated_source_exceptions(root, today=reporting_cutoff)
    exceptions = (
        conflict_exceptions
        + untagged_exceptions
        + no_dao_mortgage_exceptions
        + pending_unassigned_exceptions
        + known_property_payment_exceptions
        + future_dated_exceptions
    )
    safe_rule_counts = Counter(str(item.get("match_value") or "unknown") for item in safe_actions)
    safe_category_counts = Counter(str(item.get("target_baselane_category") or "unknown") for item in safe_actions)
    exception_reason_counts = Counter(str(item.get("reason") or "unknown") for item in exceptions)
    safe_digest = stable_digest({"safe_actions": safe_actions})
    exception_digest = stable_digest({"exceptions": exceptions})
    weekly_effective_gate_ok = weekly_cf.get("effective_ok") is True or (
        weekly_cf.get("effective_status") == "ok" and weekly_cf.get("effective_gate_status") == "ok"
    )
    weekly_status_unknown = weekly_cf.get("status") not in {"ok", None} and not weekly_effective_gate_ok
    downstream_hold = bool(exceptions or weekly_status_unknown)
    downstream_hold_targets = (
        [
            "cash_flow_statements",
            "financials_md",
            "lofty_pm_live_updates",
            "discord_property_updates",
            "owner_email",
            "telegram_transfer_reconciliation",
        ]
        if downstream_hold
        else []
    )
    next_actions: list[str] = []
    if future_dated_exceptions:
        next_actions.append(
            "Reverse or redate future-dated Baselane source journals before they reach the current ECO cash basis; retain only a forecast schedule outside the live ledger."
        )
    if pending_unassigned_exceptions:
        next_actions.append(
            "Wait for pending property-affecting debits to post, then verify their native property/tag split before rerunning source quality."
        )
    if known_property_payment_exceptions:
        next_actions.append(
            "Verify every documented parcel allocation, the complete settled debit total, and property/tag coverage for the known payment before clearing its source hold."
        )
    if no_dao_mortgage_exceptions:
        next_actions.append("Fix raw no-DAO-mortgage rows at the Baselane/source split layer before regenerating statements.")
    if conflict_exceptions or untagged_exceptions:
        next_actions.append("Resolve the remaining non-deterministic source category exceptions using live source evidence.")
    if weekly_status_unknown:
        next_actions.append("Rerun the weekly CF source gate until its effective status is ok.")
    if downstream_hold:
        next_actions.append("Keep Cash Flow Statements, FINANCIALS.md, Lofty PM, Discord, owner email, and Telegram transfer output held until ECO GL exceptions are zero.")
    return {
        "status": "ok" if not downstream_hold else "blocked",
        "generated_at": iso_z(),
        "source_month": untagged_packet.get("month") or candidate_packet.get("source_month"),
        "policy": "Auto-safe only for high-confidence known category patterns with deterministic source evidence; all other ECO GL issues block Lofty PM/email.",
        "mutation_mode": "dry_run_plan_only",
        "live_baselane_mutation_allowed": False,
        "downstream_hold": downstream_hold,
        "weekly_cf_status": weekly_cf.get("status"),
        "weekly_cf_effective_gate_status": weekly_cf.get("effective_gate_status"),
        "weekly_cf_effective_ok": weekly_cf.get("effective_ok"),
        "weekly_cf_blocked_by_effective_gate": weekly_status_unknown,
        **source_index_metadata,
        "source_index_resolved_tagged_row_count": len(resolved_source_tagged_rows),
        "resolved_source_tagged_digest": stable_digest({"rows": resolved_source_tagged_rows}),
        "downstream_hold_targets": downstream_hold_targets,
        "safe_auto_rule_candidate_ids": sorted(safe_rule_ids),
        "safe_auto_rule_count": len(safe_rule_ids),
        "safe_auto_untagged_row_count": len(safe_actions),
        "safe_auto_rule_counts": dict(sorted(safe_rule_counts.items())),
        "safe_auto_category_counts": dict(sorted(safe_category_counts.items())),
        "safe_auto_action_digest": safe_digest,
        "safe_untagged_weekly_queue_ids": sorted(str(item.get("weekly_queue_id")) for item in safe_actions),
        "safe_rule_candidate_weekly_queue_ids": sorted(safe_rule_ids),
        "conflict_exception_count": len(conflict_exceptions),
        "approved_pm_conflict_count": len(approved_pm_conflicts),
        "approved_pm_conflicts": approved_pm_conflicts,
        "untagged_exception_row_count": len(untagged_exceptions),
        "resolved_source_tagged_rows": resolved_source_tagged_rows,
        "raw_no_dao_mortgage_exception_count": len(no_dao_mortgage_exceptions),
        "raw_no_dao_mortgage_scanned_paths": no_dao_mortgage_scanned_paths,
        "raw_no_dao_mortgage_policy_properties": list(NO_DAO_MORTGAGE_PROPERTY_KEYS),
        "raw_no_dao_mortgage_policy_states": sorted(NO_DAO_MORTGAGE_STATES),
        "raw_no_dao_mortgage_min_year": NO_DAO_MORTGAGE_GUARD_MIN_YEAR,
        "pending_unassigned_material_source_exception_count": len(pending_unassigned_exceptions),
        "pending_unassigned_material_debit_minimum": PENDING_UNASSIGNED_MATERIAL_DEBIT_MINIMUM,
        "known_property_payment_split_exception_count": len(known_property_payment_exceptions),
        "known_property_payment_splits_config": str(root / KNOWN_PROPERTY_PAYMENT_SPLITS_CONFIG),
        "future_dated_source_exception_count": len(future_dated_exceptions),
        "future_dated_source_reporting_day": date.today().isoformat(),
        "future_dated_source_grace_days": SOURCE_FUTURE_DATE_GRACE_DAYS,
        "exception_count": len(exceptions),
        "exception_reason_counts": dict(exception_reason_counts.most_common(20)),
        "exception_digest": exception_digest,
        "next_actions": next_actions
        if downstream_hold
        else ["ECO GL is clean for downstream Cash Flow Statement, FINANCIALS.md, Lofty PM, Discord, owner email, and Telegram gates."],
        "artifacts": {
            "safe_actions_csv": str(reports / "baselane_ecogl_auto_safe_actions.csv"),
            "exceptions_csv": str(reports / "baselane_ecogl_data_quality_exceptions.csv"),
            "markdown": str(reports / "baselane_ecogl_data_quality_autonomy.md"),
        },
        "safe_actions": safe_actions,
        "exceptions": exceptions,
    }


def write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def write_markdown(report: dict[str, Any], path: Path) -> None:
    lines = [
        f"# ECO GL Data Quality Autonomy — {report.get('source_month') or 'unknown month'}",
        "",
        f"- Status: `{report['status']}`",
        f"- Mutation mode: `{report['mutation_mode']}`",
        f"- Downstream hold: `{report['downstream_hold']}`",
        f"- Auto-safe category rows: `{report['safe_auto_untagged_row_count']}`",
        f"- Auto-safe rules: `{report['safe_auto_rule_count']}`",
        f"- Resolved from current tagged source index: `{report['source_index_resolved_tagged_row_count']}`",
        f"- Exceptions: `{report['exception_count']}`",
        f"- Safe action digest: `{report['safe_auto_action_digest']}`",
        f"- Exception digest: `{report['exception_digest']}`",
        "",
        "## Automation Lane",
        "",
    ]
    if report["safe_auto_rule_counts"]:
        for name, count in report["safe_auto_rule_counts"].items():
            lines.append(f"- `{name}`: `{count}` row(s)")
    else:
        lines.append("- No auto-safe category mappings found.")
    lines.extend(["", "## Exception Lane", ""])
    if report["exception_reason_counts"]:
        for reason, count in report["exception_reason_counts"].items():
            lines.append(f"- `{count}`: {reason}")
    else:
        lines.append("- No ECO GL exceptions remain.")
    lines.extend(["", "## Next Actions", ""])
    lines.extend(f"- {action}" for action in report["next_actions"])
    lines.extend(["", "## Artifacts", ""])
    for label, artifact in report["artifacts"].items():
        lines.append(f"- {label}: `{artifact}`")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_outputs(report: dict[str, Any], json_path: Path, safe_csv: Path, exceptions_csv: Path, markdown: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_csv(safe_csv, SAFE_ACTION_FIELDS, report["safe_actions"])
    write_csv(exceptions_csv, EXCEPTION_FIELDS, report["exceptions"])
    write_markdown(report, markdown)


def main() -> int:
    parser = argparse.ArgumentParser(description="Separate auto-safe ECO GL cleanup from true Baselane data-quality exceptions.")
    parser.add_argument("--root", type=Path, default=Path(__file__).absolute().parents[1])
    parser.add_argument("--report", type=Path)
    parser.add_argument("--safe-csv", type=Path)
    parser.add_argument("--exceptions-csv", type=Path)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args()

    root = args.root.expanduser().resolve()
    report_path = args.report or root / "reports" / "baselane_ecogl_data_quality_autonomy.json"
    safe_csv = args.safe_csv or root / "reports" / "baselane_ecogl_auto_safe_actions.csv"
    exceptions_csv = args.exceptions_csv or root / "reports" / "baselane_ecogl_data_quality_exceptions.csv"
    markdown = args.markdown or root / "reports" / "baselane_ecogl_data_quality_autonomy.md"
    report = build_report(root)
    report["artifacts"] = {
        "safe_actions_csv": str(safe_csv),
        "exceptions_csv": str(exceptions_csv),
        "markdown": str(markdown),
    }
    write_outputs(report, report_path, safe_csv, exceptions_csv, markdown)
    print(
        json.dumps(
            {
                "status": report["status"],
                "safe_auto_untagged_row_count": report["safe_auto_untagged_row_count"],
                "exception_count": report["exception_count"],
                "downstream_hold": report["downstream_hold"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
