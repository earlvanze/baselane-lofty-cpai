#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PATTERNS = [
    ("ssn", "high", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "Possible SSN pattern."),
    ("email", "high", re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I), "Possible email address."),
    ("phone", "medium", re.compile(r"(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}\b"), "Possible phone number."),
    ("unredacted_tenant_ledger", "high", re.compile(r"\btenant ledger\b", re.I), "Tenant ledger reference in publish candidate."),
    ("internal_marker", "medium", re.compile(r"\b(?:internal-only|todo|tbd|draft|review before sending|rent_roll_gap)\b", re.I), "Draft/internal marker remains."),
    ("unchecked_checklist", "medium", re.compile(r"^\s*[-*]\s+\[\s\]", re.M), "Unchecked review checklist item remains."),
    ("secret_marker", "high", re.compile(r"\b(?:bearer\s+[A-Za-z0-9._-]+|api[_ -]?key\s*[:=]|secret\s*[:=]|password\s*[:=]|authorization\s*[:=])", re.I), "Possible secret-bearing marker."),
    (
        "disallowed_limited_financial_summary",
        "high",
        re.compile(r"This month's update is limited to verified cash-position data from Lofty and ECO records\.", re.I),
        "Monthly communications must not claim the financial summary is limited to verified cash-position data.",
    ),
    (
        "disallowed_no_tenant_ledger_disclaimer",
        "high",
        re.compile(r"No tenant ledger rows are included\.", re.I),
        "Monthly communications must not include the no-tenant-ledger limitation disclaimer.",
    ),
    (
        "disallowed_internal_ledger_control_in_summary",
        "high",
        re.compile(
            r"ECO GL Column E sum|Full property-ledger accounting position|"
            r"ECO Net DAO Funds is the full (?:DAO-attributed Column E|property General Ledger)",
            re.I,
        ),
        "Investor-facing summaries must not present the internal full-ledger control as cash.",
    ),
]

PUBLIC_CONTACT_EMAIL_ALLOWLIST = frozenset({"ecosystemspm@gmail.com"})
PENDING_LOFTY_RESERVE_RE = re.compile(
    r"^\s*-\s*Lofty operating-reserve ledger:\s*Pending reconciliation(?:\s+.*)?$",
    re.I | re.M,
)

REQUIRED_UPDATE_SUMMARY_PATTERNS = [
    (
        "missing_lofty_curr_maintenance_reserve",
        re.compile(
            r"(?:^\s*-\s*Lofty maintenance reserve balance:\s*(?:-?\$[\d,]+\.\d{2})\s*$"
            r"|^\s*-\s*Lofty Operating Reserve:\s*(?:-?\$[\d,]+\.\d{2})\s*$"
            r"|^\s*-\s*Lofty operating-reserve (?:ledger|reporting value \(live correction pending\)):\s*"
            r"(?:-?\$[\d,]+\.\d{2})(?:\s+.*)?$"
            r"|^\s*Lofty maintenance reserve balance:\s*(?:-?\$[\d,]+\.\d{2})(?:\s*\([^\n]*\))?\s*$"
            r"|^\s*-\s*Lofty-held current maintenance reserve:\s*(?:-?\$[\d,]+\.\d{2})\s*$"
            r"|^\s*Lofty Operating Cash:\s*-?\$[\d,]+\.\d{2}\s*\(Lofty curr_maintenance_reserve\)\s*$"
            r"|^\s*\|\s*Lofty Operating Cash\s*\|\s*-?\$[\d,]+\.\d{2}\s*\|"
            r"|^\s*.*\bLofty-held reserve\s+is\s*-?\$[\d,]+\.\d{2}.*$)",
            re.I | re.M,
        ),
        "Monthly update must include the Lofty-held current maintenance reserve.",
    ),
    (
        "missing_eco_net_dao_funds",
        re.compile(
            r"^\s*(?:-\s*)?ECO Net DAO Funds \(spendable cash held by ECO\):\s*\$[\d,]+\.\d{2}(?:\s*\([^\n]*\))?\s*$",
            re.I | re.M,
        ),
        "Monthly update must include verified spendable cash held by ECO for the DAO.",
    ),
]

REQUIRED_FINANCIAL_SUMMARY_PATTERNS = [
    (
        "missing_monthly_cash_position_section",
        re.compile(r"^\s*##\s+Monthly Cash Position(?:\s+\([^)]+\))?\s*$", re.I | re.M),
        "FINANCIALS.md must include the Monthly Cash Position section.",
    ),
    (
        "missing_lofty_operating_cash",
        re.compile(
            r"(?:^\s*-\s*Lofty Operating Reserve:\s*-?\$[\d,]+\.\d{2}\s*$"
            r"|^\s*-\s*Lofty operating-reserve (?:ledger|reporting value \(live correction pending\)):\s*"
            r"-?\$[\d,]+\.\d{2}(?:\s+.*)?$"
            r"|^\s*\|\s*Lofty maintenance reserve balance\s*\|\s*-?\$[\d,]+\.\d{2}\s*\|)",
            re.I | re.M,
        ),
        "FINANCIALS.md must show Lofty-held cash separately from ECO-held cash.",
    ),
    (
        "missing_eco_net_dao_funds",
        re.compile(
            r"(?:^\s*-\s*ECO Net DAO Funds \(spendable cash held by ECO\):\s*\$[\d,]+\.\d{2}\s*$"
            r"|^\s*\|\s*ECO Net DAO Funds \(spendable cash held by ECO\)\s*\|\s*\$[\d,]+\.\d{2}\s*\|)",
            re.I | re.M,
        ),
        "FINANCIALS.md must include verified spendable cash held by ECO for the DAO.",
    ),
]


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def first_existing(paths: list[str]) -> str | None:
    for raw in paths:
        path = Path(raw)
        if path.is_file():
            return str(path)
    return None


def candidate_records(candidate_packet: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not candidate_packet:
        return {}
    records: dict[str, dict[str, Any]] = {}
    for record in candidate_packet.get("records") or []:
        if not isinstance(record, dict):
            continue
        for key in ("property_path", "input_property_path"):
            if record.get(key):
                records[str(record[key])] = record
    return records


def choose_sources(record: dict[str, Any], candidates_by_property: dict[str, dict[str, Any]]) -> tuple[str | None, str | None, str]:
    candidate_record = candidates_by_property.get(str(record.get("property_path") or "")) or {}
    approved_update = first_existing(record.get("update_approved_candidates") or [])
    approved_financial = first_existing(record.get("financial_approved_candidates") or [])
    candidate_update = candidate_record.get("update_candidate")
    candidate_financial = candidate_record.get("financial_candidate")
    update_source = approved_update or (candidate_update if candidate_update and Path(str(candidate_update)).is_file() else None) or record.get("draft_path")
    financial_source = approved_financial or (candidate_financial if candidate_financial and Path(str(candidate_financial)).is_file() else None) or record.get("financials_md")
    source_mode = "approved" if approved_update or approved_financial else "candidate" if candidate_update or candidate_financial else "draft_or_canonical"
    return str(update_source) if update_source else None, str(financial_source) if financial_source else None, source_mode


def line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def exception_summary(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


def cloud_placeholder_error(path: Path) -> OSError | None:
    try:
        stat_result = path.stat()
    except OSError:
        return None
    if (
        getattr(stat_result, "st_blocks", None) == 0
        and getattr(stat_result, "st_size", 0) > 0
        and "/Dropbox/" in str(path)
    ):
        return OSError(
            11,
            f"Dropbox online-only placeholder is not hydrated: {path} (size={stat_result.st_size}, blocks=0)",
        )
    return None


def scan_text(
    path: Path,
    section: str,
    run_month: str | None = None,
    *,
    allow_pending_lofty_reserve: bool = False,
) -> list[dict[str, Any]]:
    try:
        placeholder_error = cloud_placeholder_error(path)
        if placeholder_error:
            raise placeholder_error
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError) as exc:
        return [
            {
                "section": section,
                "path": str(path),
                "line": None,
                "class": "source_read_error",
                "severity": "high",
                "message": "Expected review source exists but could not be read.",
                "error": exception_summary(exc),
            }
        ]
    lines = text.splitlines()
    findings: list[dict[str, Any]] = []
    for code, severity, pattern, message in PATTERNS:
        for match in pattern.finditer(text):
            current_line = line_number(text, match.start())
            line_text = lines[current_line - 1] if current_line - 1 < len(lines) else ""
            if code == "email" and match.group(0).lower() in PUBLIC_CONTACT_EMAIL_ALLOWLIST:
                continue
            if code == "unredacted_tenant_ledger" and (
                "no tenant ledger rows are included" in line_text.lower()
                or "pii redacted" in line_text.lower()
            ):
                continue
            findings.append(
                {
                    "section": section,
                    "path": str(path),
                    "line": current_line,
                    "class": code,
                    "severity": severity,
                    "message": message,
                }
            )
            if len([finding for finding in findings if finding["class"] == code]) >= 10:
                break
    if section == "updates":
        for code, pattern, message in REQUIRED_UPDATE_SUMMARY_PATTERNS:
            if not pattern.search(text):
                if (
                    code == "missing_lofty_curr_maintenance_reserve"
                    and allow_pending_lofty_reserve
                    and PENDING_LOFTY_RESERVE_RE.search(text)
                ):
                    continue
                findings.append(
                    {
                        "section": section,
                        "path": str(path),
                        "line": None,
                        "class": code,
                        "severity": "high",
                        "message": message,
                    }
                )
    if section == "financials":
        for code, pattern, message in REQUIRED_FINANCIAL_SUMMARY_PATTERNS:
            if not pattern.search(text):
                if (
                    code == "missing_lofty_operating_cash"
                    and allow_pending_lofty_reserve
                    and PENDING_LOFTY_RESERVE_RE.search(text)
                ):
                    continue
                findings.append(
                    {
                        "section": section,
                        "path": str(path),
                        "line": None,
                        "class": code,
                        "severity": "high",
                        "message": message,
                    }
                )
        snapshot_months = re.findall(r"(?im)^##\s+Cash Flow Snapshot\s+\((\d{4}-\d{2})\)\s*$", text)
        if run_month and snapshot_months and run_month not in snapshot_months:
            findings.append(
                {
                    "section": section,
                    "path": str(path),
                    "line": None,
                    "class": "stale_cash_flow_snapshot_month",
                    "severity": "high",
                    "message": f"FINANCIALS.md cash-flow snapshot does not include run month {run_month}.",
                }
            )
    return findings


def scan_manifest(manifest: dict[str, Any], candidate_packet: dict[str, Any] | None = None) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    high_count = 0
    medium_count = 0
    missing_count = 0
    scanned_file_count = 0
    skipped_count = 0
    source_mode_counts: dict[str, int] = {}
    candidates_by_property = candidate_records(candidate_packet)
    for record in manifest.get("records") or []:
        if not isinstance(record, dict):
            continue
        update_status = str(record.get("update_status") or "")
        financial_status = str(record.get("financial_status") or "")
        if is_excluded_status(update_status) and is_excluded_status(financial_status):
            skipped_count += 1
            continue
        candidate_record = candidates_by_property.get(str(record.get("property_path") or "")) or {}
        if candidate_record.get("live_publish_excluded") is True:
            skipped_count += 1
            continue
        update_source, financial_source, source_mode = choose_sources(record, candidates_by_property)
        source_mode_counts[source_mode] = source_mode_counts.get(source_mode, 0) + 1
        scan_record = {
            "property_name": record.get("property_name"),
            "property_path": record.get("property_path"),
            "source_mode": source_mode,
            "update_source": update_source,
            "financial_source": financial_source,
            "findings": [],
        }
        financial_summary = candidate_record.get("monthly_financial_summary")
        allow_pending_lofty_reserve = (
            isinstance(financial_summary, dict)
            and financial_summary.get("lofty_curr_maintenance_reserve") is None
        )
        for section, source in [("updates", update_source), ("financials", financial_source)]:
            if not source or not Path(source).is_file():
                missing_count += 1
                scan_record["findings"].append(
                    {
                        "section": section,
                        "path": source,
                        "line": None,
                        "class": "missing_scan_source",
                        "severity": "high",
                        "message": "Expected review source is missing.",
                    }
                )
                continue
            scanned_file_count += 1
            scan_record["findings"].extend(
                scan_text(
                    Path(source),
                    section,
                    str(manifest.get("run_month") or "") or None,
                    allow_pending_lofty_reserve=allow_pending_lofty_reserve,
                )
            )
        for finding in scan_record["findings"]:
            if finding["severity"] == "high":
                high_count += 1
            elif finding["severity"] == "medium":
                medium_count += 1
        if scan_record["findings"]:
            records.append(scan_record)
    status = "review" if high_count or missing_count or medium_count else "ok"
    return {
        "generated_at": iso_z(),
        "status": status,
        "property_count": len(manifest.get("records") or []),
        "scanned_property_count": len(manifest.get("records") or []) - skipped_count,
        "skipped_property_count": skipped_count,
        "scanned_file_count": scanned_file_count,
        "high_count": high_count,
        "medium_count": medium_count,
        "missing_count": missing_count,
        "blocker_count": high_count + medium_count + missing_count,
        "finding_property_count": len(records),
        "source_mode_counts": dict(sorted(source_mode_counts.items())),
        "records": records,
    }


def is_excluded_status(status: str) -> bool:
    return status.startswith("skipped_") or status in {
        "excluded_no_live_update_or_email",
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Lofty Monthly Review Safety Scan",
        "",
        f"- Status: `{report['status']}`",
        f"- Properties: `{report['property_count']}`",
        f"- Scanned properties: `{report.get('scanned_property_count', report['property_count'])}`",
        f"- Skipped properties: `{report.get('skipped_property_count', 0)}`",
        f"- Scanned files: `{report['scanned_file_count']}`",
        f"- High findings: `{report['high_count']}`",
        f"- Medium findings: `{report['medium_count']}`",
        f"- Missing scan sources: `{report['missing_count']}`",
        f"- Source modes: `{json.dumps(report.get('source_mode_counts', {}), sort_keys=True)}`",
        "",
        "## Findings",
        "",
    ]
    if not report["records"]:
        lines.append("No findings.")
        lines.append("")
        return "\n".join(lines)
    for record in report["records"]:
        lines.append(f"### {record['property_name']}")
        for finding in record["findings"]:
            lines.append(
                f"- `{finding['severity']}` `{finding['class']}` at `{finding['path']}`"
                + (f":{finding['line']}" if finding.get("line") else "")
                + f" — {finding['message']}"
            )
        lines.append("")
    return "\n".join(lines)


def main_args(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan monthly Lofty review artifacts for publish-safety markers before approval/send.")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--candidate-packet", type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--markdown", required=True, type=Path)
    args = parser.parse_args(argv)

    manifest = load_json(args.manifest)
    candidate_packet = load_json(args.candidate_packet) if args.candidate_packet and args.candidate_packet.is_file() else None
    report = scan_manifest(manifest, candidate_packet)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.markdown.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "property_count", "scanned_file_count", "high_count", "medium_count", "missing_count")}, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


def main() -> int:
    return main_args()


if __name__ == "__main__":
    raise SystemExit(main())
