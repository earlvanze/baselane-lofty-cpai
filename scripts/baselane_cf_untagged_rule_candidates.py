#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CSV_FIELDS = [
    "id",
    "approved",
    "match_type",
    "match_value",
    "suggested_cf_category",
    "suggested_baselane_category",
    "confidence",
    "row_count",
    "property_count",
    "amount_total",
    "sample_properties",
    "sample_merchants",
    "review_note",
]


KNOWN_RULES = [
    ("PM Fee Accrual", re.compile(r"\bPM Fee Accrual\b", re.I), "Management Fees", "Management Fees", "high", "PM fee accrual naming is deterministic."),
    ("ECO Systems PM Fee", re.compile(r"\bECO Systems LLC PM Fee\b", re.I), "Management Fees", "Management Fees", "high", "ECO Systems PM fee naming is deterministic."),
    ("DAO LLC Fee Accrual", re.compile(r"\bDAO LLC Fee Accrual\b", re.I), "Other Operating Expenses", "Other Operating Expenses", "medium", "DAO LLC fee accrual likely maps to other operating expenses; confirm unless source notes prove annual DAO/admin accrual."),
    ("Legal Fee Accrual", re.compile(r"\bLegal Fee Accrual\b", re.I), "Legal & Other Professional Fees", "Legal & Other Professional Fees", "high", "Legal fee accrual naming is deterministic."),
    ("Insurance Accrual", re.compile(r"\bInsurance Accrual\b", re.I), "Insurance", "Insurance", "high", "Insurance accrual naming is deterministic."),
    ("OSC Risk Secure", re.compile(r"\bOSC\s*-\s*RISK SECURE\b", re.I), "Insurance", "Insurance", "high", "OSC Risk Secure is the portfolio insurance/security premium pattern already mapped by CF fallback."),
    ("Tax Accrual", re.compile(r"\bTax Accrual\b", re.I), "Taxes", "Taxes", "high", "Tax accrual naming is deterministic."),
    ("County Treasurer", re.compile(r"\bCOUNTYTREASURER\b", re.I), "Taxes", "Taxes", "high", "County treasurer payment naming deterministically maps to property taxes."),
    ("Arcadia Utilities", re.compile(r"\bARCADIA\b", re.I), "Utilities", "Utilities", "high", "Arcadia utility vendor naming is deterministic and already mapped by CF fallback."),
    ("Core Utility Vendors", re.compile(r"\bFRONTIER COMM\b|\bSPECTRUM\b|\bWATER INTERNET\b|\bFPL DIRECT\b|\bENBRIDGE\b|\bHAWN ELECTRIC\b|\bCON ED OF NY\b", re.I), "Utilities", "Utilities", "high", "Utility vendor naming is deterministic for recurring property utility accounts."),
    ("Expanded Utility Vendors", re.compile(r"\bSTARLINK\b|\bPG\s*E EZ PAY\b|\bHAWAII GAS\b|\bXCEL ENERGY-PSCO\b|\bPUBLIC UTILITIES\b|\bCITY-OF-DAYTONA-BEACH\b", re.I), "Utilities", "Utilities", "high", "Utility provider naming deterministically maps to utilities."),
    ("Lofty Rental Collection Vendors", re.compile(r"\bEVOLVE VACATION\b.*\bTRANSFER\b|\bHEMLANE\b.*:[A-Z0-9]*REN\b", re.I), "Rental Income", "Rents", "high", "Rental platform collection naming deterministically maps to rents."),
    ("Bank Interest", re.compile(r"\bInterest\s+[A-Z][a-z]+\s+\d{4}\b", re.I), "Interest Received", "Interest Received", "high", "Bank interest naming deterministically maps to interest received."),
    ("SimpliSafe", re.compile(r"\bSIMPLISAFE\b", re.I), "Other Operating Expenses", "Other Operating Expenses", "high", "Security subscription naming deterministically maps to other operating expenses."),
    ("Software and Hosting Subscriptions", re.compile(r"\bHULU\b|\bNETFLIX(?:\.COM)?\b|\bHospitable\.com\b|\bHOSPITABLE\.COM\b|\bPRICELABSINC\b|\bPriceLabsInc\*Dyn\b", re.I), "Other Operating Expenses", "Other Operating Expenses", "high", "Software and hosting subscription vendor naming deterministically maps to other operating expenses."),
    ("Pest Control Vendors", re.compile(r"\bWHITE KNIGHT PEST\b|\bABSOLUTE TERMITE\b|\bAXIOM ECOPEST\b|\bRENTOKIL\b|\bEHRLICH\b", re.I), "Cleaning & Maintenance", "Cleaning & Maintenance", "high", "Pest-control vendor naming deterministically maps to cleaning and maintenance."),
    ("EPCON Lane Recurring Service", re.compile(r"\bEPCON LANE\b", re.I), "Operating Expenses", "Other Operating Expenses", "high", "Recurring EPCON Lane service charges for 1432 Sara Ave deterministically map to other operating expenses."),
    ("Home Improvement Merchants", re.compile(r"\bLOWES\b|\bHOME DEPOT\b", re.I), "Repairs", "Repairs", "medium", "Home improvement merchant likely repairs/supplies; confirm item nature."),
]


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_amount(value: object) -> float:
    text = str(value or "0").replace(",", "").strip()
    if text.startswith("(") and text.endswith(")"):
        text = f"-{text[1:-1]}"
    try:
        return float(text)
    except ValueError:
        return 0.0


def normalize(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def normalized_property_key(value: object) -> str:
    text = re.sub(r"\bpublic\b", " ", str(value or ""), flags=re.I)
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def load_json(path: Path | None) -> dict[str, Any]:
    if not path or not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def listing_update_exclusion_keys(policy: dict[str, Any] | None) -> set[str]:
    keys: set[str] = set()
    for field in ("sold_ignore_listing_updates", "operational_ignore_listing_updates"):
        values = (policy or {}).get(field) or []
        if not isinstance(values, list):
            continue
        for value in values:
            raw_value = value if isinstance(value, dict) else {}
            name = raw_value.get("address") or raw_value.get("property_name") or (value if not isinstance(value, dict) else "")
            key = normalized_property_key(name)
            if key:
                keys.add(key)
    return keys


def property_excluded(property_name: object, exclusion_keys: set[str]) -> bool:
    property_key = normalized_property_key(property_name)
    if not property_key:
        return False
    return any(property_key == key or property_key in key or key in property_key for key in exclusion_keys)


def rule_id(match_type: str, match_value: str, cf_category: str, baselane_category: str) -> str:
    material = "|".join([match_type, match_value, cf_category, baselane_category])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def stable_digest(payload: dict[str, Any]) -> str:
    material = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def candidate_digest(records: list[dict[str, Any]]) -> str:
    normalized = []
    for record in records:
        normalized.append(
            {
                "id": record.get("id"),
                "approved": bool(record.get("approved")),
                "match_type": record.get("match_type"),
                "match_value": record.get("match_value"),
                "suggested_cf_category": record.get("suggested_cf_category"),
                "suggested_baselane_category": record.get("suggested_baselane_category"),
                "confidence": record.get("confidence"),
                "row_count": record.get("row_count"),
                "property_count": record.get("property_count"),
                "amount_total": record.get("amount_total"),
            }
        )
    normalized.sort(key=lambda item: str(item.get("id") or ""))
    return stable_digest({"records": normalized})


def infer_known_rule(row: dict[str, Any]) -> tuple[str, str, str, str, str] | None:
    text = " | ".join([normalize(row.get("Merchant")), normalize(row.get("Description"))])
    note_text = normalize(row.get("Notes"))
    combined_text = f"{text} | {note_text}"
    if parse_amount(row.get("Amount")) > 0 and re.search(r"\bMay Rent\b|\brent\b", note_text, re.I):
        return (
            "Rent Deposit Notes",
            "Rental Income",
            "Rents",
            "high",
            "Positive deposit has source notes identifying rent.",
        )
    if re.search(r"\bclean(?:ing|out)?\b|\btrashout\b", note_text, re.I):
        return (
            "Cleaning Maintenance Notes",
            "Cleaning & Maintenance",
            "Cleaning & Maintenance",
            "high",
            "Source notes identify cleaning, cleanout, or trashout work.",
        )
    if re.search(r"\block sets?\b|\bsecurity door\b|\bdoor frame\b", note_text, re.I):
        return (
            "Door Security Repair Notes",
            "Repairs",
            "Repairs",
            "high",
            "Source notes identify lock, security door, or door-frame repair materials.",
        )
    if re.search(r"\bLOWE'?S\b|\bHOME DEPOT\b", text, re.I) and re.search(r"\brepair\b|\breplacement\b|\brefrigerator\b", combined_text, re.I):
        return (
            "Home Improvement Repair Notes",
            "Repairs",
            "Repairs",
            "high",
            "Home improvement merchant has source notes/text indicating repair or replacement.",
        )
    if re.search(r"\bDAO LLC Fee Accrual\b", text, re.I) and (
        re.search(r"\bAOPS-[^|]+\|dao\|", note_text, re.I)
        or re.search(r"\bAnnual DAO LLC/admin fee amortized monthly\b", note_text, re.I)
    ):
        return (
            "DAO LLC Admin Fee Accrual",
            "Other Operating Expenses",
            "Other Operating Expenses",
            "high",
            "Source notes identify this as an annual DAO LLC/admin fee amortized monthly.",
        )
    for label, pattern, cf_category, baselane_category, confidence, note in KNOWN_RULES:
        if pattern.search(text):
            return label, cf_category, baselane_category, confidence, note
    return None


def load_packet(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return data


def build_candidates(
    packet: dict[str, Any],
    min_rows: int,
    listing_update_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    notes: dict[tuple[str, str, str, str], tuple[str, str]] = {}
    exclusion_keys = listing_update_exclusion_keys(listing_update_policy)
    excluded_properties: set[str] = set()
    excluded_row_count = 0

    for row in packet.get("rows") or []:
        if not isinstance(row, dict) or row.get("review_required") is not True:
            continue
        property_name = normalize(row.get("Property"))
        if property_excluded(property_name, exclusion_keys):
            excluded_row_count += 1
            if property_name:
                excluded_properties.add(property_name)
            continue
        known = infer_known_rule(row)
        if known:
            label, cf_category, baselane_category, confidence, note = known
            key = ("known_pattern", label, cf_category, baselane_category)
            groups[key].append(row)
            notes[key] = (confidence, note)
            continue

        merchant = normalize(row.get("Merchant")) or normalize(row.get("Description")) or "(blank)"
        suggested_cf = normalize(row.get("suggested_cf_category")) or "Uncategorized"
        suggested_baselane = normalize(row.get("suggested_baselane_category"))
        if suggested_baselane and suggested_baselane != "Other Operating Expenses":
            key = ("merchant_exact", merchant, suggested_cf, suggested_baselane)
            groups[key].append(row)
            notes[key] = ("low", "Existing deterministic fallback suggested a specific Baselane category; confirm before approval.")
        else:
            key = ("merchant_review", merchant, suggested_cf, suggested_baselane or "REVIEW_REQUIRED")
            groups[key].append(row)
            notes[key] = ("review", "No safe category suggestion; review transactions individually or create a narrower rule.")

    records = []
    for (match_type, match_value, cf_category, baselane_category), rows in groups.items():
        if len(rows) < min_rows and match_type not in {"known_pattern"}:
            continue
        confidence, note = notes[(match_type, match_value, cf_category, baselane_category)]
        amount_total = round(sum(parse_amount(row.get("Amount")) for row in rows), 2)
        properties = sorted({normalize(row.get("Property")) for row in rows if normalize(row.get("Property"))})
        merchants = sorted({normalize(row.get("Merchant")) for row in rows if normalize(row.get("Merchant"))})
        records.append(
            {
                "id": rule_id(match_type, match_value, cf_category, baselane_category),
                "approved": False,
                "match_type": match_type,
                "match_value": match_value,
                "suggested_cf_category": cf_category,
                "suggested_baselane_category": baselane_category,
                "confidence": confidence,
                "row_count": len(rows),
                "property_count": len(properties),
                "amount_total": amount_total,
                "sample_properties": properties[:8],
                "sample_merchants": merchants[:8],
                "review_note": note,
            }
        )

    records.sort(key=lambda item: ({"high": 0, "medium": 1, "low": 2, "review": 3}.get(item["confidence"], 4), -item["row_count"], item["match_value"]))
    confidence_counts = Counter(record["confidence"] for record in records)
    return {
        "status": "review" if records else "ok",
        "generated_at": iso_z(),
        "source_month": packet.get("month"),
        "source_packet": packet.get("gl_csv"),
        "instructions": "Set approved=true only after human review. This file is review-only and does not mutate Baselane or workbooks.",
        "candidate_count": len(records),
        "high_confidence_count": confidence_counts.get("high", 0),
        "medium_confidence_count": confidence_counts.get("medium", 0),
        "low_confidence_count": confidence_counts.get("low", 0),
        "review_only_count": confidence_counts.get("review", 0),
        "covered_row_count": sum(record["row_count"] for record in records),
        "excluded_row_count": excluded_row_count,
        "excluded_property_count": len(excluded_properties),
        "excluded_properties": sorted(excluded_properties),
        "candidate_digest": candidate_digest(records),
        "records": records,
    }


def write_outputs(report: dict[str, Any], json_path: Path, csv_path: Path, markdown_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for record in report["records"]:
            writer.writerow(
                {
                    **{field: record.get(field, "") for field in CSV_FIELDS},
                    "sample_properties": "; ".join(record.get("sample_properties") or []),
                    "sample_merchants": "; ".join(record.get("sample_merchants") or []),
                }
            )
    lines = [
        f"# Baselane CF Untagged Rule Candidates — {report.get('source_month') or 'unknown month'}",
        "",
        f"- Status: `{report['status']}`",
        f"- Candidate rules: `{report['candidate_count']}`",
        f"- High confidence: `{report['high_confidence_count']}`",
        f"- Medium confidence: `{report['medium_confidence_count']}`",
        f"- Covered review rows: `{report['covered_row_count']}`",
        f"- Candidate digest: `{report['candidate_digest']}`",
        "",
        "These are review candidates only. They do not update Baselane tags or workbook values until a human approves and a separate apply workflow exists.",
        "",
        "## Candidates",
        "",
    ]
    for record in report["records"]:
        lines.extend(
            [
                f"### {record['match_value']}",
                f"- ID: `{record['id']}`",
                f"- Approved: `{record['approved']}`",
                f"- Match type: `{record['match_type']}`",
                f"- Suggested CF category: `{record['suggested_cf_category']}`",
                f"- Suggested Baselane category: `{record['suggested_baselane_category']}`",
                f"- Confidence: `{record['confidence']}`",
                f"- Rows/properties/amount: `{record['row_count']}` / `{record['property_count']}` / `{record['amount_total']}`",
                f"- Sample properties: `{'; '.join(record.get('sample_properties') or [])}`",
                f"- Note: {record['review_note']}",
                "",
            ]
        )
    if not report["records"]:
        lines.append("No candidate rules found.")
        lines.append("")
    markdown_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build review-only candidate rules for Baselane CF untagged GL rows.")
    parser.add_argument("--packet", required=True, type=Path)
    parser.add_argument("--json", required=True, type=Path)
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--markdown", required=True, type=Path)
    parser.add_argument("--min-rows", type=int, default=2)
    parser.add_argument("--listing-update-policy", type=Path)
    args = parser.parse_args()

    report = build_candidates(
        load_packet(args.packet),
        args.min_rows,
        listing_update_policy=load_json(args.listing_update_policy),
    )
    write_outputs(report, args.json, args.csv, args.markdown)
    print(json.dumps({key: report[key] for key in ("status", "candidate_count", "high_confidence_count", "medium_confidence_count", "covered_row_count", "candidate_digest")}, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
