#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from coownership_mortgage_policy import is_approved_madison_90_curtailment
from split_ledger_public_financials import REAL_ESTATE_BASE, normalize

ROOT = Path(__file__).absolute().parents[1]
DEFAULT_85104_RETAG_REPORT = ROOT / "reports" / "baselane_85104_preclosing_property_retag_apply.json"


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

COOWNERSHIP_POLICIES: dict[str, dict[str, Any]] = {
    "84 Madison Ave": {"first_token_sale_date": "2025-08-25", "gl_start_date": "2025-07-01", "dao_p_and_i": True},
    "86 Madison Ave": {"first_token_sale_date": "2024-12-06", "gl_start_date": "2024-11-01", "dao_p_and_i": False},
    "88 Madison Ave": {"first_token_sale_date": "2024-01-29", "gl_start_date": "2023-12-01", "dao_p_and_i": False},
    "90 Madison Ave": {"first_token_sale_date": "2024-05-14", "gl_start_date": "2024-04-01", "dao_p_and_i": False},
    "724 3rd Ave": {"first_token_sale_date": "2024-04-24", "gl_start_date": "2024-03-01", "dao_p_and_i": False},
    "85-104 Alawa Pl": {"first_token_sale_date": "2025-03-14", "gl_start_date": "2025-02-01", "dao_p_and_i": False},
    "804 S Quitman St": {"first_token_sale_date": None, "gl_start_date": None, "dao_p_and_i": True},
    "9 Country Club Ln N": {"first_token_sale_date": "2025-08-15", "gl_start_date": "2025-07-01", "dao_p_and_i": True},
}

GL_CATEGORIES_P_AND_I = {"Mortgage Principal Payments", "Mortgage Interest Payments"}
GL_CATEGORIES_ESCROW = {"Insurance", "Rental Dwelling", "Taxes", "City, State, & Local Taxes"}
GL_CATEGORIES_ESCROW_COMPAT = GL_CATEGORIES_ESCROW | {"Flood", "Escrow Payments", "General Escrow Payments", "PMI Escrows", ""}
MORTGAGE_TEXT_RE = re.compile(r"\b(mortgage|mtg|loan depot|loandepot|newrez|shellpoin|freedom|citadel|onity|phh|mortgage serv)\b", re.I)
PRINCIPAL_CURTAILMENT_RE = re.compile(r"\b(principal\s+curtail(?:ment)?|curtailment|curtail)\b", re.I)
RETAG_COMMANDS_BY_PROPERTY = {
    "85-104 Alawa Pl": (
        "BASELANE_85104_PRECLOSING_RETAG_APPLY=1 "
        "python3 scripts/baselane_85104_preclosing_property_retag.py --apply"
    )
}
GL_RELATIVE_PATH_HINTS: dict[str, tuple[str, ...]] = {
    "84 Madison Ave": (
        "NY/84 Madison Ave Public/07 - P&L & Owner Statements/ECO Systems General Ledger - 84 Madison Ave.csv",
        "NY/84 Madison Ave Albany, NY 12202/07 - P&L & Owner Statements/ECO Systems General Ledger - 84 Madison Ave.csv",
    ),
    "86 Madison Ave": (
        "NY/86 Madison Ave Public/07 - P&L & Owner Statements/ECO Systems General Ledger - 86 Madison Ave.csv",
        "NY/86 Madison Ave Albany, NY 12202/07 - P&L & Owner Statements/ECO Systems General Ledger - 86 Madison Ave.csv",
    ),
    "88 Madison Ave": (
        "NY/88 Madison Ave Public/07 - P&L & Owner Statements/ECO Systems General Ledger - 88 Madison Ave.csv",
        "NY/88 Madison Ave Albany, NY 12202/07 - P&L & Owner Statements/ECO Systems General Ledger - 88 Madison Ave.csv",
    ),
    "90 Madison Ave": (
        "NY/90 Madison Ave Public/07 - P&L & Owner Statements/ECO Systems General Ledger - 90 Madison Ave.csv",
    ),
    "724 3rd Ave": (
        "NY/724 3rd Ave, Watervliet, NY 12189/Public/07 - P&L & Owner Statements/ECO Systems General Ledger - 724 3rd Ave.csv",
    ),
    "85-104 Alawa Pl": (
        "HI/85-104 Alawa Pl Public/07 - P&L & Owner Statements/ECO Systems General Ledger - 85-104 Alawa Pl.csv",
    ),
    "804 S Quitman St": (
        "CO/804 S Quitman St, Denver, CO 80219/Public/07 - P&L & Owner Statements/ECO Systems General Ledger - 804 S Quitman St.csv",
    ),
    "9 Country Club Ln N": (
        "NY/9 Country Club Lane N Public/07 - P&L & Owner Statements/ECO Systems General Ledger - 9 Country Club Ln N.csv",
        "NY/9 Country Club Lane N/Public/07 - P&L & Owner Statements/ECO Systems General Ledger - 9 Country Club Ln N.csv",
    ),
}


def parse_date(raw: str) -> str | None:
    value = str(raw or "").strip()
    if not value or value.lower() == "date":
        return None
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_money(raw: object) -> float | None:
    text = str(raw or "").strip().replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None

def read_json(path: Path | None) -> dict[str, Any]:
    if not path or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def retag_evidence_for_property(prop: str, retag_report: dict[str, Any]) -> dict[str, Any] | None:
    if prop != "85-104 Alawa Pl" or not retag_report:
        return None
    protected_validation = (
        retag_report.get("protected_closing_row_review_validation")
        if isinstance(retag_report.get("protected_closing_row_review_validation"), dict)
        else {}
    )
    partial_command = str(retag_report.get("approval_command") or "")
    if "--allow-partial-apply-with-blocked-records" not in partial_command:
        digest = str(retag_report.get("payload_digest") or "")
        partial_command = (
            "BASELANE_85104_PRECLOSING_PARTIAL_WILL_NOT_CLEAR_VALIDATION=1 "
            "BASELANE_85104_PRECLOSING_RETAG_APPLY=1 "
            f"BASELANE_85104_PRECLOSING_RETAG_APPLY_DIGEST={digest} "
            "python3 scripts/baselane_85104_preclosing_property_retag.py --refresh-auth-report "
            "--apply --allow-partial-apply-with-blocked-records"
        ).strip()
    return {
        "source_report": retag_report.get("report") or str(DEFAULT_85104_RETAG_REPORT),
        "status": retag_report.get("status"),
        "payload_digest": retag_report.get("payload_digest"),
        "ready_count": int(retag_report.get("ready_count") or 0),
        "blocked_count": int(retag_report.get("blocked_count") or 0),
        "protected_closing_row_count": int(retag_report.get("protected_closing_row_count") or 0),
        "protected_closing_row_review_status": retag_report.get("protected_closing_row_review_status"),
        "protected_closing_row_review_required_count": protected_validation.get("required_count"),
        "protected_closing_row_reviewed_count": protected_validation.get("reviewed_count"),
        "protected_closing_row_review_blocker_count": protected_validation.get("blocker_count"),
        "protected_closing_row_review_blockers": protected_validation.get("blockers") or [],
        "guarded_apply_command": partial_command,
        "protected_review_import_command_file": (
            (retag_report.get("protected_closing_row_review_import_commands") or {}).get("path")
            if isinstance(retag_report.get("protected_closing_row_review_import_commands"), dict)
            else None
        ),
        "protected_review_csv": (
            (retag_report.get("protected_closing_row_review_csv") or {}).get("path")
            if isinstance(retag_report.get("protected_closing_row_review_csv"), dict)
            else None
        ),
    }


def shallow_gl_candidates(real_estate_base: Path) -> list[Path]:
    candidates: list[Path] = []
    patterns = (
        "*General Ledger*.csv",
        "*/*General Ledger*.csv",
        "*/*/*General Ledger*.csv",
        "*/*/Public/*General Ledger*.csv",
        "*/*/Public/07 - P&L & Owner Statements/*General Ledger*.csv",
        "*/*/07 - P&L & Owner Statements/*General Ledger*.csv",
    )
    for pattern in patterns:
        candidates.extend(path for path in real_estate_base.glob(pattern) if path.is_file())
    return candidates


def find_property_gls(real_estate_base: Path) -> dict[str, Path]:
    candidates: dict[str, list[Path]] = {prop: [] for prop in COOWNERSHIP_POLICIES}
    for prop, hints in GL_RELATIVE_PATH_HINTS.items():
        for hint in hints:
            path = real_estate_base / hint
            if path.is_file():
                candidates[prop].append(path)
    for path in shallow_gl_candidates(real_estate_base):
        if path.name.lower() == "gl rows.csv":
            continue
        text = f"{path.name} {path.parent.parent.parent.name} {path.parent.parent.name}"
        for prop in COOWNERSHIP_POLICIES:
            prop_norm = normalize(prop)
            text_norm = normalize(text)
            if prop_norm in text_norm or text_norm in prop_norm:
                candidates[prop].append(path)
    found: dict[str, Path] = {}
    for prop, paths in candidates.items():
        if not paths:
            continue
        found[prop] = min(
            paths,
            key=lambda path: (
                0 if any(part == "Public" or part.endswith(" Public") for part in path.parts) else 1,
                0 if "07 - P&L & Owner Statements" in path.parts else 1,
                0 if normalize(path.stem) == normalize(f"ECO Systems General Ledger - {prop}") else 1,
                len(path.parts),
                str(path).lower(),
            ),
        )
    return found


def row_matches_property(row: dict[str, str], prop: str) -> bool:
    value = str(row.get("Property") or row.get("property") or "")
    if value:
        prop_norm = normalize(prop)
        value_norm = normalize(value)
        if prop_norm in value_norm or value_norm in prop_norm:
            return True
    if prop == "85-104 Alawa Pl" and "85 104 alawa" in normalize(value):
        return True
    return False


def row_text(row: dict[str, str]) -> str:
    return " ".join(str(row.get(key) or "") for key in ("Merchant", "Description", "Category", "Type", "Notes"))


def principal_curtailment_text(row: dict[str, str]) -> str:
    """Return transaction-identifying fields, excluding narrative notes.

    Notes often explain a cash-settlement calculation that references an old
    curtailment. They are not evidence that the present ledger row is itself a
    DAO-attributed principal payment.
    """
    return " ".join(str(row.get(key) or "") for key in ("Merchant", "Description", "Category", "Sub-category", "Type"))


def is_approved_dao_principal_curtailment(prop: str, row: dict[str, str], amount: float) -> bool:
    """Return whether a no-P&I DAO row is an explicitly approved exception."""
    if prop != "90 Madison Ave" or amount >= 0:
        return False
    return is_approved_madison_90_curtailment(
        {
            "amount": amount,
            "note": row.get("Notes") or "",
        }
    )


def audit_property(prop: str, path: Path | None) -> dict[str, Any]:
    policy = COOWNERSHIP_POLICIES[prop]
    record: dict[str, Any] = {
        "property": prop,
        "gl_path": str(path) if path else None,
        "first_token_sale_date": policy["first_token_sale_date"],
        "gl_start_date": policy["gl_start_date"],
        # Compatibility aliases retained for downstream report consumers.
        "launch_date": policy["first_token_sale_date"],
        "dao_p_and_i_allowed": bool(policy["dao_p_and_i"]),
        "status": "ok",
        "issues": [],
        "row_count": 0,
        "column_e_sum": 0.0,
        "pre_launch_row_count": 0,
        "pre_launch_amount_sum": 0.0,
        "disallowed_mortgage_p_and_i_row_count": 0,
        "principal_curtailment_row_count": 0,
        "principal_curtailment_amount_sum": 0.0,
        "disallowed_principal_curtailment_row_count": 0,
        "mortgage_split_non_escrow_row_count": 0,
        "mortgage_artifact_review_row_count": 0,
        "pre_launch_rows": [],
        "principal_curtailment_rows": [],
    }
    if not path or not path.is_file():
        record["status"] = "blocked"
        record["issues"].append("missing_property_gl")
        return record
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        for csv_row_number, row in enumerate(csv.DictReader(handle), start=2):
            if not row_matches_property(row, prop):
                continue
            amount = parse_money(row.get("Amount"))
            if amount is None:
                continue
            record["row_count"] += 1
            record["column_e_sum"] = round(float(record["column_e_sum"]) + amount, 2)
            iso_date = parse_date(str(row.get("Date") or ""))
            if policy["gl_start_date"] and iso_date and iso_date < policy["gl_start_date"]:
                record["pre_launch_row_count"] += 1
                record["pre_launch_amount_sum"] = round(float(record["pre_launch_amount_sum"]) + amount, 2)
                record["pre_launch_rows"].append(
                    {
                        "csv_row": csv_row_number,
                        "date": row.get("Date") or "",
                        "property": row.get("Property") or "",
                        "merchant": row.get("Merchant") or "",
                        "description": row.get("Description") or "",
                        "category": row.get("Category") or "",
                        "amount": amount,
                        "notes": row.get("Notes") or "",
                    }
                )
            category = str(row.get("Category") or "").strip()
            text = row_text(row)
            mortgage_like = bool(MORTGAGE_TEXT_RE.search(text))
            # Principal curtailments are expenses. A positive internal-transfer
            # receipt whose label explains the destination is not another
            # principal payment.
            principal_curtailment_like = amount < 0 and bool(
                PRINCIPAL_CURTAILMENT_RE.search(principal_curtailment_text(row))
            )
            approved_dao_curtailment = is_approved_dao_principal_curtailment(prop, row, amount)
            if category == "Mortgage Principal Payments" or principal_curtailment_like:
                record["principal_curtailment_row_count"] += 1 if principal_curtailment_like else 0
                if principal_curtailment_like:
                    record["principal_curtailment_amount_sum"] = round(
                        float(record["principal_curtailment_amount_sum"]) + amount,
                        2,
                    )
                    record["principal_curtailment_rows"].append(
                        {
                            "csv_row": csv_row_number,
                            "date": row.get("Date") or "",
                            "property": row.get("Property") or "",
                            "merchant": row.get("Merchant") or "",
                            "description": row.get("Description") or "",
                            "category": category,
                            "amount": amount,
                            "notes": row.get("Notes") or "",
                            "dao_attributed": bool(policy["dao_p_and_i"]) or approved_dao_curtailment,
                            "approved_exception": approved_dao_curtailment,
                        }
                    )
            if (
                not policy["dao_p_and_i"]
                and category in GL_CATEGORIES_P_AND_I
                and not approved_dao_curtailment
            ):
                record["disallowed_mortgage_p_and_i_row_count"] += 1
            if (
                not policy["dao_p_and_i"]
                and principal_curtailment_like
                and not approved_dao_curtailment
            ):
                record["disallowed_principal_curtailment_row_count"] += 1
            if not policy["dao_p_and_i"] and mortgage_like and category not in GL_CATEGORIES_ESCROW_COMPAT:
                record["mortgage_artifact_review_row_count"] += 1
    if record["pre_launch_row_count"]:
        record["issues"].append("pre_launch_rows_present")
        record["next_action"] = (
            f"Retag/remove {record['pre_launch_row_count']} pre-cutoff GL rows upstream in Baselane before "
            f"{policy['gl_start_date']} for {prop}; first token sale was {policy['first_token_sale_date']}. "
            "Rerun Baselane sync, public financial split, and this validation."
        )
    if record["disallowed_mortgage_p_and_i_row_count"]:
        record["issues"].append("disallowed_mortgage_principal_interest_rows")
    if record["disallowed_principal_curtailment_row_count"]:
        record["issues"].append("disallowed_principal_curtailment_rows")
    if record["mortgage_split_non_escrow_row_count"]:
        record["issues"].append("non_escrow_mortgage_rows_for_no_dao_p_and_i_property")
    if record["issues"]:
        record["status"] = "blocked"
    return record


def build_report(real_estate_base: Path, retag_report_path: Path | None = DEFAULT_85104_RETAG_REPORT) -> dict[str, Any]:
    gls = find_property_gls(real_estate_base)
    retag_report = read_json(retag_report_path)
    records = [audit_property(prop, gls.get(prop)) for prop in COOWNERSHIP_POLICIES]
    for record in records:
        evidence = retag_evidence_for_property(str(record.get("property") or ""), retag_report)
        if evidence:
            record["prepared_upstream_retag_evidence"] = evidence
            if "pre_launch_rows_present" in record.get("issues", []):
                ready_count = int(evidence.get("ready_count") or 0)
                protected_count = int(evidence.get("protected_closing_row_count") or 0)
                reviewed_count = int(evidence.get("protected_closing_row_reviewed_count") or 0)
                record["next_action"] = (
                    f"Apply the digest-guarded partial retag for {ready_count} ready non-protected 85-104 pre-closing rows, "
                    f"then complete human review for {protected_count} protected closing-funding rows "
                    f"({reviewed_count}/{protected_count} reviewed), rerun Baselane sync, public financial split, "
                    "and this validation."
                )
    blocked = [record for record in records if record["status"] != "ok"]
    report = {
        "generated_at": iso_z(),
        "status": "ok" if not blocked else "blocked",
        "real_estate_base": str(real_estate_base),
        "prepared_retag_report": str(retag_report_path) if retag_report_path else None,
        "policy": {
            "eco_gl_semantics": "Complete DAO-attributed general ledger Column E through the as-of date, including accruals; not ECO Systems LLC bank cash only.",
            "ny_hi_start_rule": "For NY/HI co-ownerships, ECO GL rows begin on the first day of the month before the first token sale; earlier source transactions belong to their actual payer or ECO Systems LLC, not the DAO.",
            "mortgage_p_and_i_allowed_properties": [prop for prop, policy in COOWNERSHIP_POLICIES.items() if policy["dao_p_and_i"]],
            "mortgage_escrow_only_properties": [prop for prop, policy in COOWNERSHIP_POLICIES.items() if not policy["dao_p_and_i"]],
            "principal_curtailment_policy": (
                "Principal curtailment rows are DAO-attributed only for mortgage principal/interest allowed DAOs, "
                "except 90 Madison's exact configured June 2024-June 2025 50% NOI curtailments. Positive cash-transfer "
                "receipts are never counted as principal expenses. Ordinary P&I remains excluded for 86, 88, 90 "
                "Madison, 724 3rd Ave, and Alawa."
            ),
        },
        "record_count": len(records),
        "blocked_count": len(blocked),
        "blocked_properties": [record["property"] for record in blocked],
        "records": records,
    }
    actionable_commands = [
        (
            record.get("prepared_upstream_retag_evidence", {}).get("guarded_apply_command")
            if isinstance(record.get("prepared_upstream_retag_evidence"), dict)
            else None
        )
        or RETAG_COMMANDS_BY_PROPERTY[record["property"]]
        for record in blocked
        if record["property"] in RETAG_COMMANDS_BY_PROPERTY and "pre_launch_rows_present" in record.get("issues", [])
    ]
    if blocked:
        report["blocked_summary"] = [
            {
                "property": record["property"],
                "issues": record["issues"],
                "next_action": record.get("next_action"),
                "pre_launch_row_count": record.get("pre_launch_row_count"),
                "pre_launch_amount_sum": record.get("pre_launch_amount_sum"),
                "disallowed_mortgage_p_and_i_row_count": record.get("disallowed_mortgage_p_and_i_row_count"),
                "disallowed_principal_curtailment_row_count": record.get("disallowed_principal_curtailment_row_count"),
                "mortgage_artifact_review_row_count": record.get("mortgage_artifact_review_row_count"),
            }
            for record in blocked
        ]
        report["next_action"] = "; ".join(
            str(record.get("next_action") or f"Resolve {record['property']} GL policy issues upstream, rerun sync/split/validation.")
            for record in blocked
        )
    if actionable_commands:
        report["upstream_apply_commands"] = actionable_commands
        report["next_action"] = (
            f"{report.get('next_action', '').rstrip()} "
            "After review/authentication, apply prepared upstream retag command(s), rerun Baselane sync, public financial split, "
            "coownership validation, monthly transfer reconciliation, FINANCIALS.md generation, Lofty publish review, Discord post, then email gate."
        ).strip()
    return report


def write_blocker_csv(path: Path, report: dict[str, Any]) -> None:
    fieldnames = [
        "property",
        "issue",
        "csv_row",
        "date",
        "amount",
        "category",
        "merchant",
        "description",
        "notes",
        "prepared_retag_ready_count",
        "protected_closing_row_count",
        "protected_closing_row_reviewed_count",
        "protected_closing_row_review_blockers",
        "guarded_apply_command",
        "next_action",
    ]
    rows: list[dict[str, Any]] = []
    for record in report.get("records") or []:
        if not isinstance(record, dict) or record.get("status") == "ok":
            continue
        pre_launch_rows = record.get("pre_launch_rows") if isinstance(record.get("pre_launch_rows"), list) else []
        if pre_launch_rows:
            for row in pre_launch_rows:
                if not isinstance(row, dict):
                    continue
                rows.append(
                    {
                        "property": record.get("property"),
                        "issue": "pre_launch_rows_present",
                        "csv_row": row.get("csv_row"),
                        "date": row.get("date"),
                        "amount": row.get("amount"),
                        "category": row.get("category"),
                        "merchant": row.get("merchant"),
                        "description": row.get("description"),
                        "notes": row.get("notes"),
                        "prepared_retag_ready_count": (
                            record.get("prepared_upstream_retag_evidence", {}).get("ready_count")
                            if isinstance(record.get("prepared_upstream_retag_evidence"), dict)
                            else ""
                        ),
                        "protected_closing_row_count": (
                            record.get("prepared_upstream_retag_evidence", {}).get("protected_closing_row_count")
                            if isinstance(record.get("prepared_upstream_retag_evidence"), dict)
                            else ""
                        ),
                        "protected_closing_row_reviewed_count": (
                            record.get("prepared_upstream_retag_evidence", {}).get("protected_closing_row_reviewed_count")
                            if isinstance(record.get("prepared_upstream_retag_evidence"), dict)
                            else ""
                        ),
                        "protected_closing_row_review_blockers": ";".join(
                            record.get("prepared_upstream_retag_evidence", {}).get("protected_closing_row_review_blockers") or []
                        )
                        if isinstance(record.get("prepared_upstream_retag_evidence"), dict)
                        else "",
                        "guarded_apply_command": (
                            record.get("prepared_upstream_retag_evidence", {}).get("guarded_apply_command")
                            if isinstance(record.get("prepared_upstream_retag_evidence"), dict)
                            else ""
                        ),
                        "next_action": record.get("next_action"),
                    }
                )
        else:
            rows.append(
                {
                    "property": record.get("property"),
                    "issue": ";".join(record.get("issues") or []),
                    "csv_row": "",
                    "date": "",
                    "amount": "",
                    "category": "",
                    "merchant": "",
                    "description": "",
                    "notes": "",
                    "prepared_retag_ready_count": "",
                    "protected_closing_row_count": "",
                    "protected_closing_row_reviewed_count": "",
                    "protected_closing_row_review_blockers": "",
                    "guarded_apply_command": "",
                    "next_action": record.get("next_action") or report.get("next_action"),
                }
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def money(value: object) -> str:
    try:
        return f"${float(value or 0):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def build_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Co-Ownership GL Policy Review",
        "",
        f"Status: `{report.get('status')}`",
        f"Blocked count: `{report.get('blocked_count')}`",
        "",
        "## Policy",
        "",
        f"- ECO GL semantics: {report.get('policy', {}).get('eco_gl_semantics')}",
        f"- P&I allowed DAOs: `{', '.join(report.get('policy', {}).get('mortgage_p_and_i_allowed_properties') or [])}`",
        f"- Escrow-only DAOs: `{', '.join(report.get('policy', {}).get('mortgage_escrow_only_properties') or [])}`",
        f"- Principal curtailment: {report.get('policy', {}).get('principal_curtailment_policy')}",
        "",
        "## Blockers",
        "",
    ]
    blocked = [record for record in report.get("records") or [] if isinstance(record, dict) and record.get("status") != "ok"]
    if not blocked:
        lines.append("- None")
    for record in blocked:
        lines.extend(
            [
                f"### {record.get('property')}",
                "",
                f"- Issues: `{', '.join(record.get('issues') or [])}`",
                f"- Pre-launch rows: `{record.get('pre_launch_row_count')}`",
                f"- Pre-launch amount sum: `{money(record.get('pre_launch_amount_sum'))}`",
                f"- Next action: {record.get('next_action') or report.get('next_action')}",
                "",
            ]
        )
        pre_launch_rows = record.get("pre_launch_rows") if isinstance(record.get("pre_launch_rows"), list) else []
        prepared_retag = (
            record.get("prepared_upstream_retag_evidence")
            if isinstance(record.get("prepared_upstream_retag_evidence"), dict)
            else {}
        )
        if prepared_retag:
            lines.extend(
                [
                    "#### Prepared 85-104 Retag Evidence",
                    "",
                    f"- Retag report: `{prepared_retag.get('source_report')}`",
                    f"- Payload digest: `{prepared_retag.get('payload_digest')}`",
                    f"- Ready non-protected rows: `{prepared_retag.get('ready_count')}`",
                    f"- Protected closing-funding rows: `{prepared_retag.get('protected_closing_row_count')}`",
                    f"- Protected review status: `{prepared_retag.get('protected_closing_row_review_status')}`",
                    f"- Protected reviewed count: `{prepared_retag.get('protected_closing_row_reviewed_count')}/{prepared_retag.get('protected_closing_row_review_required_count')}`",
                    f"- Protected review CSV: `{prepared_retag.get('protected_review_csv')}`",
                    f"- Protected import command file: `{prepared_retag.get('protected_review_import_command_file')}`",
                    "- Protected import command: `bash reports/baselane_85104_preclosing_protected_row_review_import.requires-explicit-approval.sh`",
                    "- Protected review dispositions accepted by the importer: `untag_preclosing_row`, `exclude_from_dao_gl`, or `keep_property_tag_closing_capital`.",
                    f"- Guarded partial apply command: `{prepared_retag.get('guarded_apply_command')}`",
                    "",
                ]
            )
            blockers = prepared_retag.get("protected_closing_row_review_blockers") or []
            if blockers:
                lines.append("- Protected review blockers:")
                for blocker in blockers[:20]:
                    lines.append(f"  - `{blocker}`")
                lines.append("")
        if pre_launch_rows:
            lines.extend(["| CSV Row | Date | Amount | Category | Merchant | Notes |", "| ---: | --- | ---: | --- | --- | --- |"])
            for row in pre_launch_rows[:50]:
                if not isinstance(row, dict):
                    continue
                lines.append(
                    "| "
                    + " | ".join(
                        [
                            str(row.get("csv_row") or ""),
                            str(row.get("date") or ""),
                            money(row.get("amount")),
                            str(row.get("category") or "").replace("|", "/"),
                            str(row.get("merchant") or "").replace("|", "/"),
                            str(row.get("notes") or "").replace("|", "/"),
                        ]
                    )
                    + " |"
                )
            lines.append("")
    if report.get("upstream_apply_commands"):
        lines.extend(["## Prepared Commands", ""])
        for command in report.get("upstream_apply_commands") or []:
            lines.append(f"- `{command}`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_markdown(report), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--real-estate-base", type=Path, default=Path(REAL_ESTATE_BASE))
    parser.add_argument("--report", type=Path, default=Path("reports/coownership_gl_policy_validation.json"))
    parser.add_argument("--blocker-csv", type=Path, default=Path("reports/coownership_gl_policy_validation_blockers.csv"))
    parser.add_argument("--markdown", type=Path, default=Path("reports/coownership_gl_policy_validation.md"))
    args = parser.parse_args(argv)
    report = build_report(args.real_estate_base)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_blocker_csv(args.blocker_csv, report)
    write_markdown(args.markdown, report)
    print(f"status={report['status']} blocked={report['blocked_count']} report={args.report}")
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
