#!/usr/bin/env python3
"""Import former Aligned owner-statement detail into Baselane manual rows.

This runner is intended for the monthly Baselane close. It reads normalized
Baselane import staging CSVs produced from Aligned/AppFolio owner statement PDFs,
filters to the target month, checks Baselane for existing idempotency keys, and
optionally creates manual non-bank transactions through the authenticated CDP
GraphQL bridge.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


def workspace_root() -> Path:
    for candidate in (
        os.environ.get("WORKSPACE_ROOT"),
        "/home/digit/.openclaw/workspace",
        "/home/umbrel/.openclaw/workspace",
        str(Path(__file__).resolve().parents[1]),
    ):
        if candidate and Path(candidate).is_dir():
            return Path(candidate)
    return Path(__file__).resolve().parents[1]


ROOT = workspace_root()
DEFAULT_CONFIG = ROOT / "config" / "aligned_owner_statement_imports.json"
DEFAULT_REPORT = ROOT / "reports" / "baselane_aligned_owner_statement_import_report.json"
GRAPHQL_HELPER = ROOT / "scripts" / "baselane_graphql_via_cdp.js"

KEY_RE = re.compile(r"(?:^|\s)key=([^|\s]+)")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
DISCOVERY_MANAGER_TERMS = ("aligned properties", "appfolio")
DISCOVERY_STATEMENT_TERMS = ("owner statement", "owner packet", "cash balance", "property owner")
DISALLOWED_RICH_CATEGORY_PATTERNS = ("owner contribution", "owner distribution")
DISALLOWED_SOURCE_TERMS = ("evernest",)

TAG_IDS = {
    "Long Term Rents": "136",
    "Fees & Other Revenue": "2",
    "Transfers Between Accounts": "24",
    "Security Deposits": "29",
    "Property Management": "80",
    "Water & Sewer": "104",
    "Electric": "100",
    "Gas": "99",
    "Gas & Electric": "101",
    "Repairs Labor": "140",
    "Repairs Supplies": "139",
    "Plumbing Repairs": "85",
    "HVAC Repairs": "86",
    "Electrical Repairs": "84",
    "Appliance Repairs": "87",
    "Gardening & Landscaping": "57",
    "Cleaning & Janitorial": "52",
    "Tax Licenses & Registrations": "97",
    "Leasing Commissions": "64",
    "Advertising": "4",
}


@dataclass
class PlannedRow:
    property_short: str
    property_id: str
    row_number: int
    source_date: str
    date: str
    merchant_name: str
    description: str
    amount: Decimal
    source_type: str
    source_category: str
    source_subcategory: str
    note: str
    idempotency_key: str
    tag_id: str
    rich_category: str
    rich_tag_reason: str

    def create_input(self) -> dict[str, Any]:
        return {
            "merchantName": self.merchant_name,
            "note": self.note,
            "tagId": self.tag_id,
            "propertyId": self.property_id,
            "unitId": None,
            "entityId": None,
            "date": self.date,
            "bankAccountId": None,
            "amount": float(self.amount),
            "isReviewedByUser": True,
        }

    def report_dict(self) -> dict[str, Any]:
        out = self.create_input()
        out.update(
            {
                "property_short": self.property_short,
                "row_number": self.row_number,
                "source_date": self.source_date,
                "source_type": self.source_type,
                "source_category": self.source_category,
                "source_subcategory": self.source_subcategory,
                "description": self.description,
                "idempotency_key": self.idempotency_key,
                "richCategory": self.rich_category,
                "richTagReason": self.rich_tag_reason,
            }
        )
        return out


def note_text(note: Any) -> str:
    if isinstance(note, dict):
        return str(note.get("text") or "")
    return str(note or "")


def parse_decimal(value: str) -> Decimal:
    cleaned = str(value or "0").replace("$", "").replace(",", "").strip()
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned.strip("()")
    try:
        return Decimal(cleaned).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError(f"invalid amount {value!r}") from exc


def parse_date(value: str) -> str:
    raw = str(value or "").strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f"invalid date {value!r}")


def normalized_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


SEMANTIC_COUNTERPARTY_SUFFIXES = {"echeck", "payment", "receipt"}


def normalized_counterparty(value: Any) -> str:
    """Normalize statement role labels that can move across wrapped PDF lines."""
    tokens = normalized_text(value).split()
    while tokens and tokens[-1] in SEMANTIC_COUNTERPARTY_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def planned_live_fingerprint(row: PlannedRow) -> tuple[str, str, Decimal, str, str]:
    return (
        row.property_id,
        row.date,
        row.amount,
        normalized_counterparty(row.merchant_name),
        row.tag_id,
    )


def planned_manifest_fingerprint(row: PlannedRow) -> tuple[str, str, Decimal, str]:
    return (
        row.property_id,
        row.date,
        row.amount,
        normalized_counterparty(row.merchant_name),
    )


def manifest_row_fingerprint(row: dict[str, Any]) -> tuple[str, str, Decimal, str] | None:
    try:
        amount = parse_decimal(str(row.get("amount") or ""))
        transaction_date = parse_date(str(row.get("date") or ""))
    except ValueError:
        return None
    return (
        str(row.get("propertyId") or ""),
        transaction_date,
        amount,
        normalized_counterparty(row.get("merchantName")),
    )


def live_transaction_fingerprint(row: dict[str, Any]) -> tuple[str, str, Decimal, str, str] | None:
    try:
        amount = parse_decimal(str(row.get("amount") or ""))
        transaction_date = parse_date(str(row.get("date") or ""))
    except ValueError:
        return None
    return (
        str(row.get("propertyId") or ""),
        transaction_date,
        amount,
        normalized_counterparty(row.get("merchantName")),
        str(row.get("tagId") or ""),
    )


def planned_ledger_fingerprint(row: PlannedRow) -> tuple[str, str, Decimal, str, str]:
    return (
        normalized_text(row.property_short),
        row.date,
        row.amount,
        normalized_counterparty(row.merchant_name),
        normalized_text(row.rich_category),
    )


def existing_ledger_fingerprints(path: Path | None) -> set[tuple[str, str, Decimal, str, str]]:
    if not path or not path.is_file():
        return set()
    fingerprints: set[tuple[str, str, Decimal, str, str]] = set()
    with path.open(newline="", encoding="utf-8-sig", errors="ignore") as handle:
        for row in csv.DictReader(handle):
            if not key_from_note(str(row.get("Notes") or "")).startswith("aligned-"):
                continue
            try:
                transaction_date = parse_date(str(row.get("Date") or ""))
                amount = parse_decimal(str(row.get("Amount") or ""))
            except ValueError:
                continue
            fingerprints.add(
                (
                    normalized_text(row.get("Property")),
                    transaction_date,
                    amount,
                    normalized_counterparty(row.get("Merchant")),
                    normalized_text(row.get("Category")),
                )
            )
    return fingerprints


def key_from_note(note: str) -> str:
    match = KEY_RE.search(note)
    return match.group(1).strip() if match else ""


def disallowed_rich_category_reason(value: str) -> str | None:
    normalized = re.sub(r"[^a-z]+", " ", str(value or "").lower()).strip()
    for pattern in DISALLOWED_RICH_CATEGORY_PATTERNS:
        if pattern in normalized:
            return pattern
    return None


def disallowed_source_reason(row: dict[str, Any]) -> str | None:
    haystack = " ".join(
        str(row.get(key) or "")
        for key in ("Merchant", "Description", "Type", "Category", "Sub-category", "Notes")
    ).lower()
    for term in DISALLOWED_SOURCE_TERMS:
        if term in haystack:
            return term
    return None


def env_flag(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def dry_run_apply_refusal_report(args: argparse.Namespace, started_at: str) -> dict[str, Any]:
    return {
        "job": "baselane-aligned-owner-statement-import",
        "status": "error",
        "error": "refusing --apply while DRY_RUN or BASELANE_DRY_RUN is set",
        "apply": args.apply,
        "dry_run_env": {
            "DRY_RUN": os.environ.get("DRY_RUN"),
            "BASELANE_DRY_RUN": os.environ.get("BASELANE_DRY_RUN"),
        },
        "month": args.month,
        "started_at": started_at,
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "config": str(args.config),
        "report": str(args.report),
        "planned_count": 0,
        "to_create_count": 0,
        "created_count": 0,
        "issues": [{"issue": "dry_run_apply_refused"}],
        "rollback": {"created_transaction_ids": [], "updated_settlement_transaction_ids": []},
    }


def planned_label_guard(planned_rows: list[PlannedRow]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    violations = []
    for row in planned_rows:
        reason = disallowed_rich_category_reason(row.rich_category)
        if not reason:
            continue
        violations.append(
            {
                "idempotency_key": row.idempotency_key,
                "date": row.date,
                "amount": float(row.amount),
                "tagId": row.tag_id,
                "richCategory": row.rich_category,
                "matched_pattern": reason,
                "description": row.description,
            }
        )
    guard = {
        "status": "ok" if not violations else "review",
        "disallowed_rich_category_count": len(violations),
        "disallowed_rich_categories": violations,
        "rule": "Aligned PM-clearing rows must not use Owner Contributions or Owner Distributions style Baselane categories.",
    }
    issues = [{"issue": "planned_disallowed_rich_category", "violations": violations}] if violations else []
    return guard, issues


def classify_tag(row: dict[str, str]) -> tuple[str, str, str]:
    category = (row.get("Category") or "").strip()
    subcategory = (row.get("Sub-category") or "").strip()
    haystack = " ".join(
        [
            row.get("Merchant") or "",
            row.get("Description") or "",
            category,
            subcategory,
            row.get("Notes") or "",
        ]
    ).lower()

    def hit(*words: str) -> bool:
        return any(word in haystack for word in words)

    if (
        category == "Transfers Between Accounts"
        or "pm clearing funding" in haystack
        or "owner payment" in haystack
        or "owner contribution" in haystack
        or "owner distribution" in haystack
        or "distribution - owner" in haystack
    ):
        return TAG_IDS["Transfers Between Accounts"], "Transfers Between Accounts", "staged PM owner cash movement"
    explicit_rent = category == "Long Term Rents" and hit(
        "rent income",
        "monthly rent",
        "rent receipt",
        "prepaid rent",
    )
    if explicit_rent:
        return TAG_IDS["Long Term Rents"], "Long Term Rents", "rent income/default"
    if category in {"Fees & Other Revenue", "Other Income"}:
        return TAG_IDS["Fees & Other Revenue"], "Fees & Other Revenue", "other revenue"
    if category == "Security Deposits" or hit("security deposit"):
        return TAG_IDS["Security Deposits"], "Security Deposits", "security deposit"
    if hit("leasing commission", "lease commission", "lease fee", "placement fee"):
        return TAG_IDS["Leasing Commissions"], "Leasing Commissions", "leasing commission keyword"
    if category in {"Management Fees", "Property Management"} or hit("management fee"):
        return TAG_IDS["Property Management"], "Property Management", "management fee richer tag"
    if category == "Utilities" or hit("water", "sewer", "electric", "gas", "utility", "utilities"):
        if hit("water", "sewer"):
            return TAG_IDS["Water & Sewer"], "Water & Sewer", "utility keyword"
        if hit("electric", "illuminating"):
            return TAG_IDS["Electric"], "Electric", "utility keyword"
        if hit("gas", "dominion"):
            return TAG_IDS["Gas"], "Gas", "utility keyword"
        return TAG_IDS["Gas & Electric"], "Gas & Electric", "generic utility"
    if category in {"Taxes & Licenses", "Tax Licenses & Registrations"} or hit("tax", "license", "registration", "permit"):
        return TAG_IDS["Tax Licenses & Registrations"], "Tax Licenses & Registrations", "tax/license keyword"
    if category == "Advertising" or hit("advertising", "listing"):
        return TAG_IDS["Advertising"], "Advertising", "advertising keyword"
    if hit("landscap", "lawn", "grass", "yard", "snow"):
        return TAG_IDS["Gardening & Landscaping"], "Gardening & Landscaping", "landscaping keyword"
    if hit("clean", "janitorial"):
        return TAG_IDS["Cleaning & Janitorial"], "Cleaning & Janitorial", "cleaning keyword"
    if category == "Repairs & Maintenance" or hit("repair", "maintenance", "work order", "renovation", "reno"):
        if hit("plumb", "toilet", "sink", "drain", "faucet"):
            return TAG_IDS["Plumbing Repairs"], "Plumbing Repairs", "plumbing repair keyword"
        if hit("hvac", "furnace", "heating", "cooling", "a/c", " ac ", "air condition"):
            return TAG_IDS["HVAC Repairs"], "HVAC Repairs", "HVAC repair keyword"
        if hit("electric", "breaker", "outlet", "wiring"):
            return TAG_IDS["Electrical Repairs"], "Electrical Repairs", "electrical repair keyword"
        if hit("appliance", "stove", "refrigerator", "fridge", "washer", "dryer", "dishwasher"):
            return TAG_IDS["Appliance Repairs"], "Appliance Repairs", "appliance repair keyword"
        if hit("supply", "supplies", "material", "parts"):
            return TAG_IDS["Repairs Supplies"], "Repairs Supplies", "repair supplies keyword"
        return TAG_IDS["Repairs Labor"], "Repairs Labor", "repair labor/work order/default keyword"
    if category == "Long Term Rents":
        return TAG_IDS["Long Term Rents"], "Long Term Rents", "rent income/default"
    return TAG_IDS["Property Management"], "Property Management", "fallback for Aligned owner statement detail"


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data.get("properties"), list):
        raise ValueError(f"config missing properties list: {path}")
    return data


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:80] or "property"


def normalize_for_match(value: str) -> str:
    return re.sub(r"\s+", " ", value.lower()).strip()


def discovery_roots(item: dict[str, Any]) -> list[Path]:
    configured = item.get("search_roots") or []
    roots = [Path(str(root)) for root in configured]
    if not roots:
        for candidate in (
            os.environ.get("REAL_ESTATE_ROOT"),
            "/mnt/c/Users/digit/Dropbox/Real Estate",
            "/mnt/c/users/digit/Dropbox/Real Estate",
            "/home/digit/Dropbox/Real Estate",
        ):
            if candidate:
                roots.append(Path(candidate))
    return [root for root in roots if root.is_dir()]


def pdf_discovery_text(pdf: Path) -> str:
    pdftotext = shutil.which("pdftotext")
    if pdftotext:
        proc = subprocess.run(
            [pdftotext, "-layout", str(pdf), "-"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=90,
        )
        if proc.returncode == 0:
            return normalize_for_match(proc.stdout)

    from pypdf import PdfReader

    reader = PdfReader(pdf)
    return normalize_for_match("\n".join(page.extract_text() or "" for page in reader.pages))


def pdf_matches_disallowed_source(pdf: Path, item: dict[str, Any]) -> tuple[bool, str | None]:
    terms = [
        normalize_for_match(str(term))
        for term in (item.get("exclude_source_terms") or DISALLOWED_SOURCE_TERMS)
        if normalize_for_match(str(term))
    ]
    if not terms:
        return False, None
    name_text = normalize_for_match(pdf.name)
    for term in terms:
        if term in name_text:
            return True, term
    text = pdf_discovery_text(pdf)
    for term in terms:
        if term in text:
            return True, term
    return False, None


def discover_owner_statement_pdfs(item: dict[str, Any]) -> tuple[list[Path], list[dict[str, Any]]]:
    roots = discovery_roots(item)
    if not roots:
        return [], [{"property": item.get("property_short"), "issue": "no existing search_roots for PDF discovery"}]
    if str(item.get("discovery_mode") or "").strip().lower() == "all_pdfs_in_search_roots":
        pdfs = []
        issues: list[dict[str, Any]] = []
        seen: set[Path] = set()
        for root in roots:
            for pdf in root.rglob(str(item.get("pdf_glob") or "*.pdf")):
                if not pdf.is_file() or pdf in seen:
                    continue
                seen.add(pdf)
                try:
                    disallowed, term = pdf_matches_disallowed_source(pdf, item)
                except Exception as exc:
                    issues.append({"property": item.get("property_short"), "pdf": str(pdf), "issue": f"pdf source exclusion failed: {exc}"})
                    continue
                if disallowed:
                    issues.append(
                        {
                            "property": item.get("property_short"),
                            "pdf": str(pdf),
                            "issue": "excluded_disallowed_source_pdf",
                            "matched_term": term,
                        }
                    )
                    continue
                pdfs.append(pdf)
        return sorted(pdfs), issues

    aliases = [str(value) for value in item.get("property_aliases") or [] if str(value).strip()]
    if not aliases:
        aliases = [str(item.get("property_full") or ""), str(item.get("property_short") or "")]
    alias_needles = [normalize_for_match(value) for value in aliases if normalize_for_match(value)]
    if not alias_needles:
        return [], [{"property": item.get("property_short"), "issue": "no property aliases configured for PDF discovery"}]

    pdfs: list[Path] = []
    issues: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for root in roots:
        for pdf in root.rglob("*.pdf"):
            if pdf in seen:
                continue
            seen.add(pdf)
            try:
                text = pdf_discovery_text(pdf)
            except Exception as exc:
                issues.append({"property": item.get("property_short"), "pdf": str(pdf), "issue": f"pdf text discovery failed: {exc}"})
                continue
            if not text:
                continue
            if not any(alias in text for alias in alias_needles):
                continue
            if not any(term in text for term in DISCOVERY_STATEMENT_TERMS):
                continue
            if not any(term in text for term in DISCOVERY_MANAGER_TERMS):
                continue
            if any(term in text for term in DISALLOWED_SOURCE_TERMS):
                issues.append(
                    {
                        "property": item.get("property_short"),
                        "pdf": str(pdf),
                        "issue": "excluded_disallowed_source_pdf",
                        "matched_term": next((term for term in DISALLOWED_SOURCE_TERMS if term in text), None),
                    }
                )
                continue
            pdfs.append(pdf)
    return sorted(pdfs), issues


def stage_discovered_pdfs(pdfs: list[Path], run_dir: Path) -> Path:
    source_dir = run_dir / "pdfs"
    source_dir.mkdir(parents=True, exist_ok=True)
    for idx, pdf in enumerate(pdfs, start=1):
        suffix = pdf.suffix or ".pdf"
        staged = source_dir / f"{idx:04d}-{slug(pdf.stem)}{suffix}"
        if staged.exists():
            continue
        staged.symlink_to(pdf)
    return source_dir


def run_converter(item: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    script = Path(str(item.get("converter_script") or ""))
    if not script.is_file():
        return {"status": "skipped", "reason": f"converter missing: {script}"}
    discovered, discovery_issues = discover_owner_statement_pdfs(item)
    if not discovered:
        return {"status": "skipped", "reason": "no matching Aligned owner statement PDFs discovered", "discovery_issues": discovery_issues}
    source_dir = stage_discovered_pdfs(discovered, run_dir)
    out_dir = run_dir / "converted"
    out_dir.mkdir(parents=True, exist_ok=True)
    args = [
        sys.executable,
        str(script),
        "--source-dir",
        str(source_dir),
        "--out-dir",
        str(out_dir),
        "--property",
        str(item["property_full"]),
        "--baselane-property",
        str(item["baselane_property"]),
        "--transition-date",
        str(item.get("transition_date") or "2025-05-06"),
        "--pdf-glob",
        "*.pdf",
    ]
    if item.get("id_prefix"):
        args.extend(["--id-prefix", str(item["id_prefix"])])
    if item.get("output_stem"):
        args.extend(["--output-stem", str(item["output_stem"])])
    proc = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    stem = str(item.get("output_stem") or "")
    staging_csv = out_dir / f"{stem} - Baselane Import Staging.csv" if stem else None
    if (not staging_csv or not staging_csv.is_file()) and out_dir.is_dir():
        matches = sorted(out_dir.glob("* - Baselane Import Staging.csv"))
        staging_csv = matches[0] if matches else staging_csv
    return {
        "status": "ok" if proc.returncode == 0 else "error",
        "return_code": proc.returncode,
        "stdout_tail": proc.stdout[-2000:],
        "stderr_tail": proc.stderr[-2000:],
        "command": args,
        "discovered_pdf_count": len(discovered),
        "discovered_pdfs": [str(path) for path in discovered],
        "discovery_issues": discovery_issues,
        "run_dir": str(run_dir),
        "staging_csv": str(staging_csv) if staging_csv else None,
    }


def publish_converted_files(item: dict[str, Any], converter_result: dict[str, Any]) -> dict[str, Any]:
    run_dir = Path(str(converter_result.get("run_dir") or ""))
    stem = str(item.get("output_stem") or "").strip()
    roots = [Path(str(value)) for value in item.get("search_roots") or [] if str(value).strip()]
    target_dir = next((root for root in roots if root.is_dir()), None)
    record: dict[str, Any] = {
        "status": "skipped",
        "target_dir": str(target_dir) if target_dir else None,
        "copied_count": 0,
        "copied_files": [],
    }
    if converter_result.get("status") != "ok":
        record["reason"] = "converter_not_ok"
        return record
    if not target_dir:
        record["status"] = "review"
        record["reason"] = "no_existing_search_root"
        return record
    if not run_dir.is_dir():
        record["status"] = "review"
        record["reason"] = "converter_run_dir_missing"
        return record
    converted_dir = run_dir / "converted"
    if not converted_dir.is_dir():
        record["status"] = "review"
        record["reason"] = "converted_dir_missing"
        return record
    candidates = sorted(converted_dir.glob(f"{stem}*.csv")) if stem else sorted(converted_dir.glob("*.csv"))
    copied = []
    for source in candidates:
        target = target_dir / source.name
        shutil.copy2(source, target)
        copied.append({"source": str(source), "target": str(target), "size": target.stat().st_size})
    record.update({
        "status": "ok",
        "copied_count": len(copied),
        "copied_files": copied,
    })
    return record


def load_staging_rows(item: dict[str, Any], month: str, batch: str) -> tuple[list[PlannedRow], list[dict[str, Any]]]:
    staging_csv = Path(str(item.get("_resolved_staging_csv") or item.get("staging_csv") or ""))
    if not staging_csv.is_file():
        return [], [{"property": item.get("property_short"), "issue": f"missing staging_csv: {staging_csv}"}]
    issues: list[dict[str, Any]] = []
    planned: list[PlannedRow] = []
    with staging_csv.open(newline="", encoding="utf-8-sig") as handle:
        for row_number, row in enumerate(csv.DictReader(handle), start=1):
            try:
                iso_date = parse_date(row.get("Date", ""))
            except ValueError as exc:
                issues.append({"property": item.get("property_short"), "row_number": row_number, "issue": str(exc)})
                continue
            if iso_date[:7] != month:
                continue
            disallowed_source = disallowed_source_reason(row)
            if disallowed_source:
                issues.append(
                    {
                        "property": item.get("property_short"),
                        "row_number": row_number,
                        "issue": "disallowed_source_term",
                        "term": disallowed_source,
                    }
                )
                continue
            note = row.get("Notes") or ""
            key = key_from_note(note)
            if not key:
                issues.append({"property": item.get("property_short"), "row_number": row_number, "issue": "missing key= idempotency note"})
                continue
            try:
                amount = parse_decimal(row.get("Amount", ""))
            except ValueError as exc:
                issues.append({"property": item.get("property_short"), "row_number": row_number, "issue": str(exc)})
                continue
            tag_id, rich_category, reason = classify_tag(row)
            if f"batch={batch}" not in note:
                note = f"{note} | batch={batch} | reversible_manifest={batch}"
            planned.append(
                PlannedRow(
                    property_short=str(item.get("property_short") or item.get("property_full") or ""),
                    property_id=str(item["baselane_property_id"]),
                    row_number=row_number,
                    source_date=row.get("Date") or "",
                    date=iso_date,
                    merchant_name=(row.get("Merchant") or "Aligned Properties").strip() or "Aligned Properties",
                    description=(row.get("Description") or "").strip(),
                    amount=amount,
                    source_type=(row.get("Type") or "").strip(),
                    source_category=(row.get("Category") or "").strip(),
                    source_subcategory=(row.get("Sub-category") or "").strip(),
                    note=note,
                    idempotency_key=key,
                    tag_id=tag_id,
                    rich_category=rich_category,
                    rich_tag_reason=reason,
                )
            )
    return planned, issues


def run_graphql(payload: dict[str, Any]) -> dict[str, Any]:
    if not GRAPHQL_HELPER.exists():
        raise FileNotFoundError(f"missing GraphQL helper: {GRAPHQL_HELPER}")
    helper_timeout_ms = int(os.environ.get("BASELANE_GQL_TIMEOUT_MS") or "60000")
    command_timeout_ms = int(os.environ.get("BASELANE_GQL_COMMAND_TIMEOUT_MS") or "15000")
    helper_timeout_seconds = max(15, (helper_timeout_ms + (2 * command_timeout_ms) + 10000 + 999) // 1000)
    is_read_only = not str(payload.get("query") or "").lstrip().lower().startswith("mutation")
    attempts = 3 if is_read_only else 1
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle)
        handle.flush()
        payload_path = handle.name
    try:
        proc: subprocess.CompletedProcess[str] | None = None
        data: dict[str, Any] | None = None
        for attempt in range(1, attempts + 1):
            try:
                proc = subprocess.run(
                    ["node", str(GRAPHQL_HELPER), payload_path],
                    check=False,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=os.environ.copy(),
                    timeout=helper_timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                stdout = exc.stdout or ""
                stderr = exc.stderr or ""
                parts = [f"GraphQL helper timed out after {helper_timeout_seconds}s"]
                if stderr:
                    parts.append(str(stderr).strip())
                if stdout:
                    parts.append(str(stdout).strip())
                raise RuntimeError("\n".join(parts)) from exc
            if proc.returncode != 0:
                break
            try:
                data = json.loads(proc.stdout)
                break
            except json.JSONDecodeError as exc:
                if attempt == attempts:
                    stderr = proc.stderr.strip()
                    detail = f"{exc}; helper stderr: {stderr}" if stderr else str(exc)
                    raise RuntimeError(
                        f"GraphQL helper returned invalid JSON after {attempts} attempts: {detail}"
                    ) from exc
    finally:
        Path(payload_path).unlink(missing_ok=True)
    if proc is None:
        raise RuntimeError("GraphQL helper did not run")
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        stdout = proc.stdout.strip()
        parts = []
        if stderr:
            parts.append(stderr)
        if stdout:
            parts.append(stdout)
        raise RuntimeError("\n".join(parts) or f"GraphQL helper rc={proc.returncode}")
    if data is None:
        raise RuntimeError("GraphQL helper returned no data")
    if data.get("errors"):
        raise RuntimeError(json.dumps(data["errors"], indent=2))
    return data


def query_transactions(search: str, page_limit: int = 25) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page = 1
    total = None
    while True:
        payload = {
            "operationName": "Transactions",
            "variables": {
                "input": {
                    "sort": {"direction": "DESC", "field": "date"},
                    "filter": {"search": search, "isHidden": False, "isDeleted": False},
                    "page": page,
                    "pageLimit": page_limit,
                }
            },
            "query": """
            query Transactions($input: SortsAndFilters) {
              transactions(input: $input) {
                total
                data {
                  id
                  amount
                  date
                  merchantName
                  bankAccountId
                  propertyId
                  tagId
                  note
                  isManual
                  hidden
                  isDeleted
                }
              }
            }
            """,
        }
        result = run_graphql(payload)["data"]["transactions"]
        total = int(result.get("total") or 0)
        batch = result.get("data") or []
        rows.extend(batch)
        if len(rows) >= total or not batch:
            break
        page += 1
    return rows


def create_transaction(row: PlannedRow) -> dict[str, Any]:
    payload = {
        "operationName": "createTransaction",
        "variables": row.create_input(),
        "query": """
        mutation createTransaction($merchantName: String!, $note: String!, $tagId: ID, $propertyId: ID, $unitId: ID, $entityId: Int, $date: String!, $bankAccountId: ID, $amount: Float!, $isReviewedByUser: Boolean) {
          createTransaction(input: { merchantName: $merchantName note: $note tagId: $tagId propertyId: $propertyId unitId: $unitId entityId: $entityId date: $date bankAccountId: $bankAccountId amount: $amount isReviewedByUser: $isReviewedByUser }) {
            id
            merchantName
            bankAccountId
            amount
            isManual
            tagId
            propertyId
            date
            note
          }
        }
        """,
    }
    return run_graphql(payload)["data"]["createTransaction"]


def update_transactions(inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not inputs:
        return []
    payload = {
        "operationName": "UpdateTransaction",
        "variables": {"input": inputs},
        "query": """
        mutation UpdateTransaction($input: [UpdateTransaction!]) {
          updateTransactions(input: $input) {
            id
            note
            propertyId
            tagId
            amount
          }
        }
        """,
    }
    return run_graphql(payload)["data"]["updateTransactions"]


def existing_keys_for_prefix(prefix: str) -> tuple[set[str], list[dict[str, Any]]]:
    transactions = query_transactions(prefix)
    keys = {key for row in transactions if (key := key_from_note(note_text(row.get("note"))))}
    return keys, transactions


def keys_from_manifest_path(path: Path) -> tuple[set[str], dict[str, Any]]:
    record: dict[str, Any] = {"path": str(path), "status": "missing", "key_count": 0}
    if not path.is_file():
        return set(), record
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data.get("rows") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            rows = []
        manifest_keys = {str(row.get("idempotency_key") or "") for row in rows if isinstance(row, dict) and row.get("idempotency_key")}
        record.update({"status": "ok", "key_count": len(manifest_keys)})
        return manifest_keys, record
    except Exception as exc:
        record.update({"status": "error", "error": str(exc)})
        return set(), record


def existing_keys_from_manifests(
    items: list[dict[str, Any]],
    extra_manifest_dirs: list[Path] | None = None,
) -> tuple[set[str], list[dict[str, Any]], set[tuple[str, str, Decimal, str]]]:
    keys: set[str] = set()
    manifests: list[dict[str, Any]] = []
    fingerprints: set[tuple[str, str, Decimal, str]] = set()
    seen_paths: set[Path] = set()
    seen_dirs: set[Path] = set()

    def load_path(path: Path) -> None:
        if path in seen_paths:
            return
        seen_paths.add(path)
        manifest_keys, record = keys_from_manifest_path(path)
        keys.update(manifest_keys)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            rows = payload.get("rows") if isinstance(payload, dict) else payload
            for row in rows if isinstance(rows, list) else []:
                if isinstance(row, dict) and (fingerprint := manifest_row_fingerprint(row)) is not None:
                    fingerprints.add(fingerprint)
        except Exception:
            pass
        manifests.append(record)

    for item in items:
        for raw_path in item.get("existing_manifest_paths") or []:
            load_path(Path(str(raw_path)))
        for raw_dir in item.get("existing_manifest_dirs") or []:
            manifest_dir = Path(str(raw_dir))
            if manifest_dir in seen_dirs:
                continue
            seen_dirs.add(manifest_dir)
            dir_record: dict[str, Any] = {"path": str(manifest_dir), "status": "missing_dir", "key_count": 0, "manifest_count": 0}
            if not manifest_dir.is_dir():
                manifests.append(dir_record)
                continue
            before = len(keys)
            manifest_files = sorted(manifest_dir.glob("*.json"))
            for path in manifest_files:
                load_path(path)
            dir_record.update({"status": "ok_dir", "key_count": len(keys) - before, "manifest_count": len(manifest_files)})
            manifests.append(dir_record)
    for manifest_dir in extra_manifest_dirs or []:
        if manifest_dir in seen_dirs:
            continue
        seen_dirs.add(manifest_dir)
        dir_record = {"path": str(manifest_dir), "status": "missing_dir", "key_count": 0, "manifest_count": 0}
        if not manifest_dir.is_dir():
            manifests.append(dir_record)
            continue
        before = len(keys)
        manifest_files = sorted(manifest_dir.glob("*.json"))
        for path in manifest_files:
            load_path(path)
        dir_record.update({"status": "ok_dir", "key_count": len(keys) - before, "manifest_count": len(manifest_files)})
        manifests.append(dir_record)
    return keys, manifests, fingerprints


def existing_keys_from_ledger(path: Path | None) -> tuple[set[str], dict[str, Any]]:
    record: dict[str, Any] = {"path": str(path) if path else None, "status": "not_configured", "key_count": 0}
    if not path:
        return set(), record
    if not path.is_file():
        record["status"] = "missing"
        return set(), record
    keys: set[str] = set()
    with path.open(newline="", encoding="utf-8-sig", errors="ignore") as handle:
        for row in csv.DictReader(handle):
            key = key_from_note(str(row.get("Notes") or ""))
            if key:
                keys.add(key)
    record.update({"status": "ok", "key_count": len(keys)})
    return keys, record


def planned_row_to_ledger_row(row: PlannedRow, fieldnames: list[str]) -> dict[str, str]:
    ledger_row = {field: "" for field in fieldnames}
    values = {
        "Account": "",
        "Date": row.date,
        "Merchant": row.merchant_name,
        "Description": row.description,
        "Amount": f"{row.amount:.2f}",
        "Type": row.source_type,
        "Category": row.source_category,
        "Sub-category": row.source_subcategory,
        "Property": row.property_short,
        "Unit": "",
        "Notes": row.note,
    }
    ledger_row.update({field: values[field] for field in fieldnames if field in values})
    return ledger_row


def stage_rows_into_ledger(path: Path | None, rows: list[PlannedRow]) -> dict[str, Any]:
    record: dict[str, Any] = {
        "path": str(path) if path else None,
        "status": "not_configured",
        "staged_count": 0,
        "staged_amount_sum": "0.00",
    }
    if not path:
        return record
    if not path.is_file():
        record.update({"status": "missing"})
        return record
    with path.open(newline="", encoding="utf-8-sig", errors="ignore") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
    if not fieldnames:
        record.update({"status": "review", "reason": "ledger_missing_headers"})
        return record
    required = {"Date", "Merchant", "Description", "Amount", "Type", "Category", "Sub-category", "Property", "Unit", "Notes"}
    missing = sorted(required - set(fieldnames))
    if missing:
        record.update({"status": "review", "reason": "ledger_missing_required_columns", "missing_columns": missing})
        return record
    amount_sum = sum((row.amount for row in rows), Decimal("0.00"))
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        for row in rows:
            writer.writerow(planned_row_to_ledger_row(row, fieldnames))
    record.update(
        {
            "status": "ok",
            "staged_count": len(rows),
            "staged_amount_sum": f"{amount_sum:.2f}",
            "staged_keys": [row.idempotency_key for row in rows],
        }
    )
    return record


def candidate_settlement_relabels(month: str, property_ids: set[str], batch: str) -> list[dict[str, Any]]:
    candidates = []
    seen: set[str] = set()
    for search in ("Aligned", "AppFolio"):
        for row in query_transactions(search):
            tx_id = str(row.get("id") or "")
            if not tx_id or tx_id in seen:
                continue
            seen.add(tx_id)
            tx_date = str(row.get("date") or "")
            if tx_date[:7] != month:
                continue
            if str(row.get("propertyId") or "") not in property_ids:
                continue
            if row.get("bankAccountId") in (None, ""):
                continue
            try:
                amount = Decimal(str(row.get("amount") or "0"))
            except InvalidOperation:
                continue
            if amount <= 0:
                continue
            note = note_text(row.get("note"))
            if key_from_note(note):
                continue
            if str(row.get("tagId") or "") == TAG_IDS["Transfers Between Accounts"]:
                continue
            new_note = note.strip()
            suffix = f"Aligned/AppFolio PM cash settlement relabeled to transfer | batch={batch}"
            new_note = f"{new_note} | {suffix}" if new_note else suffix
            candidates.append(
                {
                    "id": tx_id,
                    "amount": float(amount),
                    "date": tx_date,
                    "merchantName": row.get("merchantName"),
                    "bankAccountId": row.get("bankAccountId"),
                    "propertyId": row.get("propertyId"),
                    "oldTagId": row.get("tagId"),
                    "oldNote": note,
                    "update": {"id": tx_id, "tagId": TAG_IDS["Transfers Between Accounts"], "note": new_note},
                }
            )
    return candidates


def default_month() -> str:
    if os.environ.get("RUN_MONTH"):
        return os.environ["RUN_MONTH"]
    today = date.today()
    return f"{today.year:04d}-{today.month:02d}"


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_created_manifest(path: Path, report: dict[str, Any]) -> None:
    rows = []
    for item in report.get("created") or []:
        input_row = item.get("input") if isinstance(item.get("input"), dict) else {}
        rows.append(
            {
                "baselane_id": item.get("baselane_id"),
                "idempotency_key": item.get("idempotency_key"),
                "propertyId": input_row.get("propertyId"),
                "date": input_row.get("date"),
                "amount": input_row.get("amount"),
                "merchantName": input_row.get("merchantName"),
                "note": input_row.get("note"),
            }
        )
    manifest = {
        "job": "baselane-aligned-owner-statement-created-transactions-manifest",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "batch": report.get("batch"),
        "month": report.get("month"),
        "apply": report.get("apply"),
        "created_count": len(rows),
        "settlement_relabel_updated_count": report.get("settlement_relabel_updated_count", 0),
        "updated_settlement_transaction_ids": report.get("rollback", {}).get("updated_settlement_transaction_ids", []),
        "created_transaction_ids": report.get("rollback", {}).get("created_transaction_ids", []),
        "settlement_relabel_rollback_rows": [
            {
                "id": item.get("id"),
                "propertyId": item.get("propertyId"),
                "date": item.get("date"),
                "amount": item.get("amount"),
                "merchantName": item.get("merchantName"),
                "oldTagId": item.get("oldTagId"),
                "oldNote": item.get("oldNote"),
                "newTagId": (item.get("update") or {}).get("tagId") if isinstance(item.get("update"), dict) else None,
                "newNote": (item.get("update") or {}).get("note") if isinstance(item.get("update"), dict) else None,
            }
            for item in report.get("settlement_relabel_candidates") or []
        ],
        "rows": rows,
    }
    write_report(path, manifest)


def expected_plan_check(path: Path | None, month: str, planned_rows: list[PlannedRow]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    check: dict[str, Any] = {
        "status": "not_configured",
        "path": str(path) if path else None,
        "month": month,
    }
    if not path:
        return check, []
    if not path.is_file():
        check["status"] = "missing"
        return check, [{"issue": "expected_plan_queue_missing", "path": str(path)}]
    try:
        queue = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        check.update({"status": "unreadable", "error": str(exc)})
        return check, [{"issue": "expected_plan_queue_unreadable", "path": str(path), "error": str(exc)}]

    plan = queue.get("expected_plan") if isinstance(queue.get("expected_plan"), dict) else {}
    months = plan.get("months") if isinstance(plan.get("months"), dict) else {}
    expected = months.get(month) if isinstance(months.get(month), dict) else None
    if expected is None:
        check.update({"status": "missing_month", "available_months": sorted(months)})
        return check, [{"issue": "expected_plan_month_missing", "month": month, "path": str(path)}]

    planned_keys = [row.idempotency_key for row in planned_rows]
    expected_keys = [str(key) for key in expected.get("idempotency_keys") or [] if str(key)]
    expected_tags = expected.get("tags_by_key") if isinstance(expected.get("tags_by_key"), dict) else {}
    planned_key_set = set(planned_keys)
    expected_key_set = set(expected_keys)
    planned_by_key = {row.idempotency_key: row for row in planned_rows}
    planned_amount = sum((row.amount for row in planned_rows), Decimal("0.00")).quantize(Decimal("0.01"))
    try:
        expected_amount = Decimal(str(expected.get("amount_total"))).quantize(Decimal("0.01"))
    except Exception:
        expected_amount = None
    expected_count = expected.get("count")

    issues: list[dict[str, Any]] = []
    duplicate_expected_keys = sorted(key for key, count in Counter(expected_keys).items() if count > 1)
    missing_keys = sorted(expected_key_set - planned_key_set)
    extra_keys = sorted(planned_key_set - expected_key_set)
    if duplicate_expected_keys:
        issues.append({"issue": "expected_plan_duplicate_keys", "month": month, "keys": duplicate_expected_keys})
    if expected_count is not None and int(expected_count) != len(planned_rows):
        issues.append(
            {
                "issue": "expected_plan_count_mismatch",
                "month": month,
                "expected_count": int(expected_count),
                "planned_count": len(planned_rows),
            }
        )
    if expected_amount is not None and expected_amount != planned_amount:
        issues.append(
            {
                "issue": "expected_plan_amount_mismatch",
                "month": month,
                "expected_amount": f"{expected_amount:.2f}",
                "planned_amount": f"{planned_amount:.2f}",
            }
        )
    if missing_keys or extra_keys:
        issues.append(
            {
                "issue": "expected_plan_key_mismatch",
                "month": month,
                "missing_keys": missing_keys,
                "extra_keys": extra_keys,
            }
        )
    tag_mismatches: list[dict[str, Any]] = []
    expected_tag_violations: list[dict[str, Any]] = []
    for key, expected_tag in expected_tags.items():
        if not isinstance(expected_tag, dict):
            continue
        row = planned_by_key.get(str(key))
        expected_tag_id = str(expected_tag.get("tagId") or "")
        expected_rich_category = str(expected_tag.get("richCategory") or "")
        expected_reason = disallowed_rich_category_reason(expected_rich_category)
        if expected_reason:
            expected_tag_violations.append(
                {
                    "idempotency_key": str(key),
                    "richCategory": expected_rich_category,
                    "tagId": expected_tag_id,
                    "matched_pattern": expected_reason,
                }
            )
        if row is None:
            continue
        if expected_tag_id and str(row.tag_id) != expected_tag_id:
            tag_mismatches.append(
                {
                    "idempotency_key": str(key),
                    "field": "tagId",
                    "expected": expected_tag_id,
                    "planned": str(row.tag_id),
                }
            )
        if expected_rich_category and row.rich_category != expected_rich_category:
            tag_mismatches.append(
                {
                    "idempotency_key": str(key),
                    "field": "richCategory",
                    "expected": expected_rich_category,
                    "planned": row.rich_category,
                }
            )
    if tag_mismatches:
        issues.append({"issue": "expected_plan_tag_mismatch", "month": month, "mismatches": tag_mismatches})
    if expected_tag_violations:
        issues.append(
            {
                "issue": "expected_plan_disallowed_rich_category",
                "month": month,
                "violations": expected_tag_violations,
            }
        )

    check.update(
        {
            "status": "ok" if not issues else "review",
            "expected_count": expected_count,
            "planned_count": len(planned_rows),
            "expected_amount": f"{expected_amount:.2f}" if expected_amount is not None else None,
            "planned_amount": f"{planned_amount:.2f}",
            "expected_key_count": len(expected_keys),
            "planned_key_count": len(planned_keys),
            "missing_key_count": len(missing_keys),
            "extra_key_count": len(extra_keys),
            "duplicate_expected_key_count": len(duplicate_expected_keys),
            "expected_tag_count": len(expected_tags),
            "tag_mismatch_count": len(tag_mismatches),
            "expected_disallowed_rich_category_count": len(expected_tag_violations),
        }
    )
    return check, issues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("BASELANE_ALIGNED_OWNER_IMPORT_CONFIG", DEFAULT_CONFIG)))
    parser.add_argument("--month", default=os.environ.get("ALIGNED_OWNER_IMPORT_MONTH") or default_month())
    parser.add_argument("--report", type=Path, default=Path(os.environ.get("BASELANE_ALIGNED_OWNER_IMPORT_REPORT", DEFAULT_REPORT)))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--convert", action="store_true", help="discover PDFs by content and regenerate staging CSVs before importing")
    parser.add_argument("--skip-baselane-query", action="store_true")
    parser.add_argument("--skip-settlement-relabels", action="store_true")
    parser.add_argument("--stage-ledger", type=Path, default=None, help="Append planned non-duplicate detail rows to a local/staged GL CSV without writing Baselane live data")
    parser.add_argument("--publish-converted", action="store_true", help="Copy regenerated converter CSV outputs back to the configured Public source folder")
    parser.add_argument("--manifest-dir", type=Path, default=None, help="Directory for durable per-batch created transaction manifests")
    parser.add_argument("--expected-plan-queue", type=Path, default=None, help="Queued backfill file containing reviewed expected_plan keys for this month")
    parser.add_argument(
        "--property-id",
        action="append",
        default=[],
        help="Limit this run to one Baselane property id. May be passed more than once.",
    )
    args = parser.parse_args()

    if not MONTH_RE.match(args.month):
        raise SystemExit(f"--month must be YYYY-MM, got {args.month!r}")

    started_at = datetime.now(timezone.utc).isoformat()
    if args.apply and (env_flag("DRY_RUN") or env_flag("BASELANE_DRY_RUN")):
        report = dry_run_apply_refusal_report(args, started_at)
        write_report(args.report, report)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 1

    batch = f"aligned-monthly-import-{args.month}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    config = load_config(args.config)
    enabled = [item for item in config["properties"] if item.get("enabled", True)]
    property_id_filter = {str(value).strip() for value in args.property_id if str(value).strip()}
    if property_id_filter:
        enabled = [item for item in enabled if str(item.get("baselane_property_id") or "").strip() in property_id_filter]
        if not enabled:
            issues_report = {
                "job": "baselane-aligned-owner-statement-import",
                "status": "review",
                "apply": args.apply,
                "month": args.month,
                "batch": batch,
                "started_at": started_at,
                "ended_at": datetime.now(timezone.utc).isoformat(),
                "config": str(args.config),
                "report": str(args.report),
                "property_id_filter": sorted(property_id_filter),
                "enabled_property_count": 0,
                "planned_count": 0,
                "to_create_count": 0,
                "created_count": 0,
                "issues": [{"issue": "property_id_filter_matched_no_enabled_properties"}],
            }
            write_report(args.report, issues_report)
            print(json.dumps(issues_report, indent=2, sort_keys=True))
            return 2

    converter_results = []
    run_base = args.report.parent / "aligned-owner-statement-import" / batch
    resolved_items: list[dict[str, Any]] = []
    if args.convert:
        for item in enabled:
            property_slug = slug(str(item.get("property_short") or item.get("property_full") or "property"))
            result = run_converter(item, run_base / property_slug)
            if args.publish_converted:
                result = {**result, "publish_converted": publish_converted_files(item, result)}
            converter_results.append({"property": item.get("property_short") or item.get("property_full"), **result})
            resolved = dict(item)
            if result.get("status") == "ok" and result.get("staging_csv"):
                resolved["_resolved_staging_csv"] = result["staging_csv"]
            resolved_items.append(resolved)
    else:
        resolved_items = [dict(item) for item in enabled]

    planned_rows: list[PlannedRow] = []
    issues: list[dict[str, Any]] = []
    for item in resolved_items:
        rows, row_issues = load_staging_rows(item, args.month, batch)
        planned_rows.extend(rows)
        issues.extend(row_issues)

    duplicate_keys = [key for key, count in Counter(row.idempotency_key for row in planned_rows).items() if count > 1]
    if duplicate_keys:
        issues.extend({"issue": "duplicate planned idempotency key", "idempotency_key": key} for key in duplicate_keys)
    label_guard, label_issues = planned_label_guard(planned_rows)
    issues.extend(label_issues)
    plan_check, plan_issues = expected_plan_check(args.expected_plan_queue, args.month, planned_rows)
    issues.extend(plan_issues)

    extra_manifest_dirs = [args.manifest_dir] if args.manifest_dir else []
    (
        manifest_existing_keys,
        existing_manifest_reports,
        existing_manifest_fingerprints,
    ) = existing_keys_from_manifests(
        resolved_items,
        extra_manifest_dirs=extra_manifest_dirs,
    )
    existing_keys: set[str] = set(manifest_existing_keys)
    stage_ledger_existing_keys, stage_ledger_existing_report = existing_keys_from_ledger(args.stage_ledger)
    existing_keys.update(stage_ledger_existing_keys)
    existing_key_transactions: list[dict[str, Any]] = []
    settlement_candidates: list[dict[str, Any]] = []
    query_error = None
    property_ids = {row.property_id for row in planned_rows} | {str(item.get("baselane_property_id")) for item in enabled if item.get("baselane_property_id")}
    if not args.skip_baselane_query and (planned_rows or args.apply or not args.skip_settlement_relabels):
        try:
            for prefix in sorted({str(item.get("id_prefix") or "").strip() for item in enabled if item.get("id_prefix")}):
                keys, txs = existing_keys_for_prefix(prefix)
                existing_keys.update(keys)
                existing_key_transactions.extend(txs)
            if not args.skip_settlement_relabels:
                settlement_candidates = candidate_settlement_relabels(args.month, property_ids, batch)
        except Exception as exc:
            query_error = str(exc)
            if args.apply:
                report = {
                    "job": "baselane-aligned-owner-statement-import",
                    "status": "error",
                    "error": query_error,
                    "apply": args.apply,
                    "month": args.month,
                    "started_at": started_at,
                    "ended_at": datetime.now(timezone.utc).isoformat(),
                    "config": str(args.config),
                    "report": str(args.report),
                }
                write_report(args.report, report)
                print(json.dumps(report, indent=2, sort_keys=True))
                return 1

    existing_live_fingerprints = {
        fingerprint
        for row in existing_key_transactions
        if (fingerprint := live_transaction_fingerprint(row)) is not None
    }
    existing_stage_fingerprints = existing_ledger_fingerprints(args.stage_ledger)
    skipped_existing = [row for row in planned_rows if row.idempotency_key in existing_keys]
    skipped_semantic_duplicates = [
        row
        for row in planned_rows
        if row.idempotency_key not in existing_keys
        and (
            planned_live_fingerprint(row) in existing_live_fingerprints
            or planned_manifest_fingerprint(row) in existing_manifest_fingerprints
            or planned_ledger_fingerprint(row) in existing_stage_fingerprints
        )
    ]
    skipped_semantic_duplicate_keys = {row.idempotency_key for row in skipped_semantic_duplicates}
    to_create = [
        row
        for row in planned_rows
        if row.idempotency_key not in existing_keys
        and row.idempotency_key not in skipped_semantic_duplicate_keys
    ]

    created: list[dict[str, Any]] = []
    updated_settlements: list[dict[str, Any]] = []
    stage_ledger_report = stage_rows_into_ledger(args.stage_ledger, to_create) if not issues else {
        "path": str(args.stage_ledger) if args.stage_ledger else None,
        "status": "skipped_due_to_issues" if args.stage_ledger else "not_configured",
        "staged_count": 0,
        "staged_amount_sum": "0.00",
    }
    if args.stage_ledger and stage_ledger_report.get("status") not in {"ok", "not_configured"}:
        issues.append({"issue": "stage_ledger_failed", **stage_ledger_report})
    apply_error = None
    if args.apply and not issues:
        try:
            for row in to_create:
                result = create_transaction(row)
                created.append(
                    {
                        "baselane_id": result.get("id"),
                        "idempotency_key": row.idempotency_key,
                        "input": row.create_input(),
                        "result": result,
                    }
                )
            if settlement_candidates:
                updated_settlements = update_transactions([item["update"] for item in settlement_candidates])
        except Exception as exc:
            apply_error = str(exc)

    status = "ok"
    if issues:
        status = "review"
    if query_error:
        status = "review"
    if apply_error:
        status = "error"
    amount_sum = sum((row.amount for row in to_create), Decimal("0.00"))
    manifest_path = None
    if args.manifest_dir:
        manifest_path = args.manifest_dir / f"{batch}-created-transactions-manifest.json"

    report = {
        "job": "baselane-aligned-owner-statement-import",
        "status": status,
        "apply": args.apply,
        "month": args.month,
        "batch": batch,
        "started_at": started_at,
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "config": str(args.config),
        "property_id_filter": sorted(property_id_filter),
        "created_manifest_path": str(manifest_path) if manifest_path and args.apply else None,
        "converter_results": converter_results,
        "enabled_property_count": len(enabled),
        "planned_count": len(planned_rows),
        "planned_amount_sum": f"{sum((row.amount for row in planned_rows), Decimal('0.00')):.2f}",
        "existing_key_count": len(existing_keys),
        "existing_manifest_key_count": len(manifest_existing_keys),
        "existing_manifest_reports": existing_manifest_reports,
        "stage_ledger_existing": stage_ledger_existing_report,
        "stage_ledger": stage_ledger_report,
        "label_guard": label_guard,
        "expected_plan_check": plan_check,
        "skipped_existing_count": len(skipped_existing),
        "skipped_semantic_duplicate_count": len(skipped_semantic_duplicates),
        "to_create_count": len(to_create),
        "to_create_amount_sum": f"{amount_sum:.2f}",
        "created_count": len(created),
        "settlement_relabel_candidate_count": len(settlement_candidates),
        "settlement_relabel_updated_count": len(updated_settlements),
        "query_error": query_error,
        "apply_error": apply_error,
        "issues": issues,
        "planned_rows": [row.report_dict() for row in planned_rows],
        "skipped_existing_keys": [row.idempotency_key for row in skipped_existing],
        "skipped_semantic_duplicate_keys": [
            row.idempotency_key for row in skipped_semantic_duplicates
        ],
        "created": created,
        "settlement_relabel_candidates": settlement_candidates,
        "settlement_relabel_updates": updated_settlements,
        "rollback": {
            "created_transaction_ids": [str(item.get("baselane_id")) for item in created if item.get("baselane_id")],
            "updated_settlement_transaction_ids": [str(item.get("id")) for item in settlement_candidates],
            "batch_note": batch,
        },
    }
    write_report(args.report, report)
    if args.apply and manifest_path and (created or updated_settlements):
        write_created_manifest(manifest_path, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    if apply_error:
        return 1
    return 0 if status == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
