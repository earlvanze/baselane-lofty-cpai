#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


DEFAULT_GL_PATH = Path("/mnt/c/Users/digit/Dropbox/Projects/assetrail/ECO Systems General Ledger.csv")
MONTHLY_SCOPE_REPORTS = {
    "monthly_readiness": "reports/baselane_financials_monthly_readiness.json",
    "monthly_review_candidate_packet": "reports/baselane_financials_monthly_review_candidate_packet.json",
    "lofty_pm_publish": "reports/baselane_financials_monthly_lofty_pm_publish.json",
    "owner_email_packet": "reports/baselane_monthly_owner_email_packet.json",
}


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def token_signature(value: object) -> set[str]:
    return {token for token in normalize(value).split() if len(token) > 1}


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def flatten_text(value: Any) -> str:
    parts: list[str] = []
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif item is not None:
            parts.append(str(item))
    return normalize(" ".join(parts))


def load_ledger_properties(path: Path) -> dict[str, int]:
    if not path.is_file():
        return {}
    counts: dict[str, int] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            prop = (row.get("Property") or "").strip()
            if prop:
                counts[prop] = counts.get(prop, 0) + 1
    return counts


def closest_ledger_properties(property_name: str, ledger_counts: dict[str, int], limit: int = 5) -> list[dict[str, Any]]:
    target_norm = normalize(property_name)
    target_tokens = token_signature(property_name)
    scored: list[tuple[float, str]] = []
    for ledger_property in ledger_counts:
        ledger_norm = normalize(ledger_property)
        ledger_tokens = token_signature(ledger_property)
        overlap = len(target_tokens & ledger_tokens) / max(len(target_tokens | ledger_tokens), 1)
        ratio = SequenceMatcher(None, target_norm, ledger_norm).ratio()
        scored.append((max(overlap, ratio), ledger_property))
    scored.sort(reverse=True)
    return [
        {
            "property": ledger_property,
            "score": round(score, 4),
            "row_count": ledger_counts[ledger_property],
        }
        for score, ledger_property in scored[:limit]
    ]


def property_mentioned(property_name: str, report_text: str) -> bool:
    norm = normalize(property_name)
    if norm and norm in report_text:
        return True
    tokens = norm.split()
    if len(tokens) >= 3:
        anchor = " ".join(tokens[:3])
        return anchor in report_text
    return False


def build_report(root: Path, month: str, gl_path: Path) -> dict[str, Any]:
    audit_path = root / "reports" / "cf_statement_sync" / f"audit_{month}.json"
    audit = read_json(audit_path)
    records = audit if isinstance(audit, list) else []
    ledger_counts = load_ledger_properties(gl_path)
    monthly_text_by_report = {
        key: flatten_text(read_json(root / rel_path))
        for key, rel_path in MONTHLY_SCOPE_REPORTS.items()
    }

    no_gl_records: list[dict[str, Any]] = []
    reconciled_no_gl_records: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        hard_no_gl_error = record.get("error") == "No matching property found in Baselane GL"
        reconciled_no_gl = record.get("no_gl_property_match") is True and not record.get("error")
        if not hard_no_gl_error and not reconciled_no_gl:
            continue
        summary = record.get("summary") if isinstance(record.get("summary"), dict) else {}
        property_name = normalize(summary.get("property"))
        exact_ledger_row_count = ledger_counts.get(summary.get("property") or "", 0)
        monthly_scope_hits = {
            key: property_mentioned(property_name, report_text)
            for key, report_text in monthly_text_by_report.items()
        }
        output_record = {
            "property": property_name,
            "cf_file": summary.get("file"),
            "error": record.get("error"),
            "warning": record.get("warning"),
            "reconciled_to_zero_raw_gl_rows": reconciled_no_gl,
            "exact_ledger_row_count": exact_ledger_row_count,
            "appears_in_monthly_scope": any(monthly_scope_hits.values()),
            "monthly_scope_hits": monthly_scope_hits,
            "closest_ledger_properties": closest_ledger_properties(property_name, ledger_counts),
        }
        if hard_no_gl_error:
            no_gl_records.append(output_record)
        else:
            reconciled_no_gl_records.append(output_record)

    return {
        "status": "review" if no_gl_records else "ok",
        "generated_at": iso_z(),
        "month": month,
        "audit_report": str(audit_path),
        "source_gl": str(gl_path),
        "source_gl_property_count": len(ledger_counts),
        "no_gl_property_match_count": len(no_gl_records),
        "active_monthly_scope_count": sum(1 for record in no_gl_records if record["appears_in_monthly_scope"]),
        "reconciled_no_gl_property_count": len(reconciled_no_gl_records),
        "reconciled_no_gl_active_monthly_scope_count": sum(1 for record in reconciled_no_gl_records if record["appears_in_monthly_scope"]),
        "reconciled_records": reconciled_no_gl_records,
        "records": no_gl_records,
    }


def write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "property",
        "appears_in_monthly_scope",
        "exact_ledger_row_count",
        "monthly_readiness",
        "monthly_review_candidate_packet",
        "lofty_pm_publish",
        "owner_email_packet",
        "closest_ledger_property",
        "closest_ledger_score",
        "reconciled_to_zero_raw_gl_rows",
        "cf_file",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            closest = (record.get("closest_ledger_properties") or [{}])[0]
            hits = record.get("monthly_scope_hits") or {}
            writer.writerow(
                {
                    "property": record.get("property"),
                    "appears_in_monthly_scope": record.get("appears_in_monthly_scope"),
                    "exact_ledger_row_count": record.get("exact_ledger_row_count"),
                    "monthly_readiness": hits.get("monthly_readiness"),
                    "monthly_review_candidate_packet": hits.get("monthly_review_candidate_packet"),
                    "lofty_pm_publish": hits.get("lofty_pm_publish"),
                    "owner_email_packet": hits.get("owner_email_packet"),
                    "closest_ledger_property": closest.get("property"),
                    "closest_ledger_score": closest.get("score"),
                    "reconciled_to_zero_raw_gl_rows": record.get("reconciled_to_zero_raw_gl_rows"),
                    "cf_file": record.get("cf_file"),
                }
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize CF workbooks that have no matching Baselane GL property.")
    parser.add_argument("--root", type=Path, default=Path(__file__).absolute().parents[1])
    parser.add_argument("--month", default="2026-06")
    parser.add_argument("--gl-path", type=Path, default=DEFAULT_GL_PATH)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    out_path = args.out or args.root / "reports" / "cf_statement_sync" / f"no_gl_property_match_{args.month}.json"
    csv_path = out_path.with_suffix(".csv")
    report = build_report(args.root, args.month, args.gl_path)
    report["csv_report"] = str(csv_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_csv(csv_path, [*report["records"], *report["reconciled_records"]])
    print(json.dumps({k: report[k] for k in ["status", "month", "no_gl_property_match_count", "active_monthly_scope_count", "reconciled_no_gl_property_count", "csv_report"]}, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
