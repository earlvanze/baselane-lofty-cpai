#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any


CSV_FIELDS = [
    "property",
    "file",
    "row",
    "label",
    "action",
    "type",
    "cf_value",
    "gl_total",
    "diff",
    "severity",
    "next_action",
]


ACTION_NEXT_ACTION = {
    "cf_has_value_gl_empty": "Verify whether the CF value is a valid manual/accrual item; if valid, add/confirm Baselane accrual tagging, otherwise clear stale CF value with review.",
    "review_accrual_in_baselane": "Formula cell already has a value; verify Baselane accrual/category tagging rather than overwriting the formula.",
    "fill_from_gl": "Review GL-backed value and approve guarded CF workbook update from Baselane data.",
    "overwrite": "Review non-zero CF/GL delta and approve overwrite only if Baselane GL is source of truth for this row.",
}


ACTION_SEVERITY = {
    "cf_has_value_gl_empty": "high",
    "review_accrual_in_baselane": "high",
    "fill_from_gl": "medium",
    "overwrite": "medium",
}


def normalize_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def flatten_conflicts(audit_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for audit_row in audit_rows:
        if not isinstance(audit_row, dict):
            continue
        summary = audit_row.get("summary") or {}
        property_name = summary.get("property") or audit_row.get("property") or "Unknown"
        file_path = summary.get("file") or audit_row.get("file") or ""
        for conflict in audit_row.get("conflicts") or []:
            if not isinstance(conflict, dict):
                continue
            action = conflict.get("action") or conflict.get("type") or "unknown"
            rows.append(
                {
                    "property": property_name,
                    "file": file_path,
                    "row": normalize_value(conflict.get("row")),
                    "label": normalize_value(conflict.get("label")),
                    "action": action,
                    "type": normalize_value(conflict.get("type")),
                    "cf_value": normalize_value(conflict.get("cf_value")),
                    "gl_total": normalize_value(conflict.get("gl_total")),
                    "diff": normalize_value(conflict.get("diff")),
                    "severity": ACTION_SEVERITY.get(action, "medium"),
                    "next_action": ACTION_NEXT_ACTION.get(action, "Review CF/GL mismatch before relying on monthly financial reporting."),
                }
            )
    return rows


def build_packet(audit_path: Path) -> dict[str, Any]:
    audit_data = json.loads(audit_path.read_text(encoding="utf-8"))
    audit_rows = audit_data if isinstance(audit_data, list) else []
    rows = flatten_conflicts(audit_rows)
    action_counts = Counter(row["action"] for row in rows)
    severity_counts = Counter(row["severity"] for row in rows)
    property_counts = Counter(row["property"] for row in rows)
    high_rows = [row for row in rows if row["severity"] == "high"]
    return {
        "status": "review" if rows else "ok",
        "audit_report": str(audit_path),
        "conflict_count": len(rows),
        "high_count": len(high_rows),
        "medium_count": len(rows) - len(high_rows),
        "property_count": len(property_counts),
        "action_counts": dict(sorted(action_counts.items())),
        "severity_counts": dict(sorted(severity_counts.items())),
        "property_counts": dict(property_counts.most_common()),
        "rows": rows,
    }


def write_outputs(packet: dict[str, Any], json_path: Path, csv_path: Path, markdown_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(packet, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in packet["rows"]:
            writer.writerow({field: row.get(field, "") for field in CSV_FIELDS})
    lines = [
        "# Baselane CF Conflict Review",
        "",
        f"- Status: {packet['status']}",
        f"- Conflicts: {packet['conflict_count']}",
        f"- High priority: {packet['high_count']}",
        f"- Medium priority: {packet['medium_count']}",
        f"- Properties: {packet['property_count']}",
        f"- CSV: `{csv_path}`",
        "",
        "## Action Counts",
    ]
    for action, count in packet["action_counts"].items():
        lines.append(f"- {action}: {count}")
    lines.extend(["", "## Top Properties"])
    for property_name, count in list(packet["property_counts"].items())[:30]:
        lines.append(f"- {property_name}: {count}")
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build structured review artifacts for Baselane CF/GL conflicts.")
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--json", required=True, type=Path)
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--markdown", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    packet = build_packet(args.audit)
    write_outputs(packet, args.json, args.csv, args.markdown)
    print(json.dumps({k: packet[k] for k in ["status", "conflict_count", "high_count", "medium_count", "property_count"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
