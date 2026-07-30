#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import baselane_ecogl_source_fix_evidence as evidence


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def match_action(action: dict[str, str], source_row: dict[str, str]) -> bool:
    action_date = evidence.parsed_date(action.get("date"))
    source_date = evidence.parsed_date(source_row.get("ISODate") or source_row.get("Date"))
    dates_match = (
        action_date is not None
        and source_date is not None
        and action_date.date() == source_date.date()
    )
    return (
        evidence.normalize(source_row.get("Property")) == evidence.normalize(action.get("property"))
        and dates_match
        and evidence.normalize(source_row.get("Merchant")) == evidence.normalize(action.get("merchant"))
        and evidence.amount_number(source_row.get("Amount")) == evidence.amount_number(action.get("amount"))
    )


def categorized_matches(action: dict[str, str], source_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    matches = [row for row in source_rows if match_action(action, row)]
    return [row for row in matches if evidence.category_is_usable(evidence.category_value(row))]


def verification_source(root: Path, export_csv: Path | None = None) -> tuple[Path | None, str]:
    if export_csv:
        return export_csv, "explicit_export"
    source_index = root / "reports" / "baselane_source_transaction_index.csv"
    if source_index.is_file():
        return source_index, "current_source_index"
    latest_export = evidence.latest_export_csv(root)
    return latest_export, "latest_export"


def build_report(root: Path, actions_csv: Path, export_csv: Path | None = None) -> dict[str, Any]:
    actions = read_csv(actions_csv)
    selected_source, source_kind = verification_source(root, export_csv)
    source_rows = read_csv(selected_source) if selected_source else []
    results: list[dict[str, Any]] = []
    for action in actions:
        matches = [row for row in source_rows if match_action(action, row)]
        categorized = [row for row in matches if evidence.category_is_usable(evidence.category_value(row))]
        category_counts = Counter(evidence.category_value(row) for row in categorized)
        action_type = str(action.get("action_type") or "")
        if action_type == "reverse_or_delete_future_dated_source_journal":
            if matches:
                status = "still_future_dated"
            else:
                status = "verified_fixed"
        elif not matches:
            status = "missing_in_latest_export"
        elif not categorized:
            status = "still_uncategorized"
        elif len(category_counts) == 1:
            status = "verified_fixed"
        else:
            status = "conflicting_categories"
        results.append(
            {
                "id": action.get("id"),
                "property": action.get("property"),
                "date": action.get("date"),
                "amount": action.get("amount"),
                "merchant": action.get("merchant"),
                "action_type": action_type,
                "status": status,
                "match_count": len(matches),
                "categorized_match_count": len(categorized),
                "category_counts": dict(sorted(category_counts.items())),
                "verified_category": next(iter(category_counts)) if len(category_counts) == 1 else "",
            }
        )
    status_counts = Counter(str(item.get("status") or "unknown") for item in results)
    remaining_count = len(results) - status_counts.get("verified_fixed", 0)
    return {
        "generated_at": iso_z(),
        "status": "ok" if remaining_count == 0 else "review",
        "actions_csv": str(actions_csv),
        "latest_export_csv": str(selected_source) if selected_source else "",
        "verification_source_csv": str(selected_source) if selected_source else "",
        "verification_source_kind": source_kind,
        "action_count": len(actions),
        "verified_fixed_count": status_counts.get("verified_fixed", 0),
        "remaining_count": remaining_count,
        "status_counts": dict(sorted(status_counts.items())),
        "downstream_hold": remaining_count > 0,
        "next_action": (
            "Rerun weekly file updates; all queued ECO GL rows are categorized in the latest Baselane export."
            if remaining_count == 0
            else "Fix remaining queued ECO GL source categories in Baselane, export again, then rerun weekly file updates."
        ),
        "results": results,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# ECO GL Source-Fix Verifier",
        "",
        f"- Status: `{report['status']}`",
        f"- Actions: `{report['action_count']}`",
        f"- Verified fixed: `{report['verified_fixed_count']}`",
        f"- Remaining: `{report['remaining_count']}`",
        f"- Downstream hold: `{str(report['downstream_hold']).lower()}`",
        f"- Latest export: `{report.get('latest_export_csv') or 'none'}`",
        f"- Next action: {report['next_action']}",
        "",
        "## Results",
        "",
    ]
    for item in report.get("results") or []:
        lines.append(
            f"- `{item.get('status')}` — `{item.get('id')}` — {item.get('property')} — "
            f"{item.get('date')} — {item.get('amount')} — {item.get('merchant')} — categories `{item.get('category_counts')}`"
        )
    if not report.get("results"):
        lines.append("- No queued source-fix actions.")
    lines.append("")
    return "\n".join(lines)


def write_json(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify queued ECO GL source fixes against the latest Baselane export.")
    parser.add_argument("--root", type=Path, default=Path(__file__).absolute().parents[1])
    parser.add_argument("--actions-csv", type=Path)
    parser.add_argument("--export-csv", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args()

    root = args.root
    actions_csv = args.actions_csv or root / "reports" / "baselane_ecogl_source_fix_actions.csv"
    report_path = args.report or root / "reports" / "baselane_ecogl_source_fix_verifier.json"
    markdown_path = args.markdown or root / "reports" / "baselane_ecogl_source_fix_verifier.md"
    report = build_report(root, actions_csv, args.export_csv)
    write_json(report_path, report)
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"status": report["status"], "verified_fixed_count": report["verified_fixed_count"], "remaining_count": report["remaining_count"]}, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
