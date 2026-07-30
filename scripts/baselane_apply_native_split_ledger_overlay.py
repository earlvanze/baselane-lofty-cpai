#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


ROOT = Path(__file__).absolute().parents[1]
DEFAULT_LEDGER = Path("/mnt/c/Users/digit/Dropbox/Projects/assetrail/ECO Systems General Ledger.csv")
DEFAULT_PLAN = ROOT / "reports" / "baselane_native_split_plan.json"
DEFAULT_REPORT = ROOT / "reports" / "baselane_native_split_ledger_overlay_report.json"
DEFAULT_BACKUP_ROOT = ROOT / "reports" / "native_split_ledger_overlay_backups"
DEFAULT_RULE_IDS = (
    "madison_morgan_linen_4_5_6_5",
    "madison_spectrum_6958_equal",
    "madison_netflix_equal",
    "madison_hulu_equal",
    "hospitable_april_2026_listing_weights",
    "pricelabs_april_2026_listing_weights",
)
PROPERTY_LABELS = {
    "22164 Umland Circle": "22164 Umland",
    "27 Pillar Ln": "27 Pillar",
    "22 W Main St": "22 W Main",
    "9 Country Club Ln N": "9 Country Club",
    "84 Madison Ave": "84 Madison",
    "86 Madison Ave": "86 Madison",
    "88 Madison Ave": "88 Madison",
    "90 Madison Ave": "90 Madison",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize(value: object) -> str:
    return " ".join(str(value or "").strip().lower().split())


def amount(value: object) -> Decimal:
    return Decimal(str(value or "0").replace("$", "").replace(",", "").strip() or "0")


def amount_text(value: object) -> str:
    return str(amount(value).quantize(Decimal("0.01"))).rstrip("0").rstrip(".")


def row_digest(rows: list[dict[str, str]]) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), [{key: str(value or "") for key, value in row.items()} for row in reader]


def write_csv_atomic(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclude Account column from all outputs (privacy)
    filtered_fieldnames = [f for f in fieldnames if f != "Account"]
    filtered_rows = [{k: v for k, v in row.items() if k != "Account"} for row in rows]
    with tempfile.NamedTemporaryFile("w", newline="", encoding="utf-8", dir=str(path.parent), delete=False) as handle:
        temp_path = Path(handle.name)
        writer = csv.DictWriter(handle, fieldnames=filtered_fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(filtered_rows)
    temp_path.replace(path)


def load_plan(path: Path, rule_ids: set[str]) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    records = data.get("records") if isinstance(data, dict) else []
    return [
        record
        for record in records or []
        if isinstance(record, dict)
        and record.get("rule") in rule_ids
        and (
            record.get("status") == "ready_native_split"
            or record.get("planned_status") == "ready_native_split"
            or record.get("apply_status") == "applied"
        )
        and record.get("splits")
    ]


def parent_matches(row: dict[str, str], record: dict[str, Any]) -> bool:
    return (
        normalize(row.get("Date")) == normalize(record.get("date"))
        and normalize(row.get("Merchant")) == normalize(record.get("merchant"))
        and normalize(row.get("Description")) == normalize(record.get("description"))
        and normalize(row.get("Property")) == normalize(record.get("source_property"))
        and amount(row.get("Amount")) == amount(record.get("amount"))
    )


def split_label(property_name: str) -> str:
    return PROPERTY_LABELS.get(property_name, property_name)


def child_merchant(record: dict[str, Any], split: dict[str, Any], total_weight: int) -> str:
    base = str(record.get("merchant") or "").upper()
    return f"{base} | {split_label(str(split.get('property') or ''))} - {split.get('weight')}/{total_weight}"


def build_child_rows(parent: dict[str, str], record: dict[str, Any]) -> list[dict[str, str]]:
    splits = [split for split in record.get("splits") or [] if isinstance(split, dict)]
    total_weight = sum(int(split.get("weight") or 0) for split in splits)
    child_rows: list[dict[str, str]] = []
    for split in splits:
        child = dict(parent)
        child["Merchant"] = child_merchant(record, split, total_weight)
        child["Amount"] = amount_text(split.get("amount"))
        child["Property"] = str(split.get("property") or "")
        child["Category"] = str(split.get("category") or record.get("category") or "")
        child_rows.append(child)
    return child_rows


def child_matches(row: dict[str, str], record: dict[str, Any], split: dict[str, Any], total_weight: int) -> bool:
    return (
        normalize(row.get("Date")) == normalize(record.get("date"))
        and normalize(row.get("Description")) == normalize(record.get("description"))
        and normalize(row.get("Merchant")) == normalize(child_merchant(record, split, total_weight))
        and normalize(row.get("Property")) == normalize(split.get("property"))
        and amount(row.get("Amount")) == amount(split.get("amount"))
        and normalize(row.get("Category")) == normalize(split.get("category") or record.get("category"))
    )


def existing_child_count(rows: list[dict[str, str]], record: dict[str, Any]) -> int:
    splits = [split for split in record.get("splits") or [] if isinstance(split, dict)]
    total_weight = sum(int(split.get("weight") or 0) for split in splits)
    return sum(1 for split in splits if any(child_matches(row, record, split, total_weight) for row in rows))


def apply_overlay(fieldnames: list[str], rows: list[dict[str, str]], records: list[dict[str, Any]]) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    output_rows = rows
    actions: list[dict[str, Any]] = []
    for record in records:
        parent_indices = [index for index, row in enumerate(output_rows) if parent_matches(row, record)]
        child_count = existing_child_count(output_rows, record)
        expected_child_count = len(record.get("splits") or [])
        action = {
            "id": record.get("id"),
            "rule": record.get("rule"),
            "baselane_id": record.get("baselane_id"),
            "date": record.get("date"),
            "amount": record.get("amount"),
            "source_property": record.get("source_property"),
            "expected_child_count": expected_child_count,
            "existing_child_count": child_count,
            "parent_match_count": len(parent_indices),
            "status": "review",
        }
        if not parent_indices and child_count == expected_child_count:
            action["status"] = "already_overlayed"
        elif len(parent_indices) == 1 and child_count == 0:
            parent_index = parent_indices[0]
            children = build_child_rows(output_rows[parent_index], record)
            output_rows = output_rows[:parent_index] + children + output_rows[parent_index + 1 :]
            action["status"] = "overlay_applied"
            action["child_count"] = len(children)
        elif parent_indices and child_count == expected_child_count:
            parent_index_set = set(parent_indices)
            output_rows = [row for index, row in enumerate(output_rows) if index not in parent_index_set]
            action["status"] = "removed_duplicate_parent"
            action["removed_parent_count"] = len(parent_indices)
        elif not parent_indices:
            action["status"] = "blocked_parent_missing"
        else:
            action["status"] = "blocked_ambiguous_existing_children"
        actions.append(action)
    return output_rows, actions


def build_report(
    ledger: Path,
    plan: Path,
    rule_ids: set[str],
    apply: bool,
    backup_root: Path | None = None,
) -> dict[str, Any]:
    issues: list[str] = []
    if not ledger.is_file():
        issues.append(f"missing_ledger:{ledger}")
    if not plan.is_file():
        issues.append(f"missing_plan:{plan}")
    if issues:
        return {
            "generated_at": now_iso(),
            "status": "review",
            "mutation_mode": "apply" if apply else "dry_run",
            "issues": issues,
            "issue_count": len(issues),
        }
    fieldnames, rows = read_csv(ledger)
    records = load_plan(plan, rule_ids)
    output_rows, actions = apply_overlay(fieldnames, rows, records)
    blocked = [action for action in actions if str(action.get("status", "")).startswith("blocked")]
    applied = [action for action in actions if action.get("status") in {"overlay_applied", "removed_duplicate_parent"}]
    backup_path = None
    if apply and applied and not blocked:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_root = backup_root or DEFAULT_BACKUP_ROOT / stamp
        backup_path = backup_root / ledger.name
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        backup_path.write_bytes(ledger.read_bytes())
        write_csv_atomic(ledger, fieldnames, output_rows)
    elif apply and blocked:
        issues.append("blocked_actions_present_no_write")
    return {
        "generated_at": now_iso(),
        "status": "ok" if not issues and not blocked else "review",
        "mutation_mode": "apply" if apply else "dry_run",
        "ledger": str(ledger),
        "ledger_sha256": file_sha256(ledger),
        "plan": str(plan),
        "rule_ids": sorted(rule_ids),
        "record_count": len(records),
        "action_count": len(actions),
        "applied_count": len(applied),
        "already_overlayed_count": sum(1 for action in actions if action.get("status") == "already_overlayed"),
        "blocked_count": len(blocked),
        "input_row_count": len(rows),
        "output_row_count": len(output_rows),
        "input_digest": row_digest(rows),
        "output_digest": row_digest(output_rows),
        "output_written": bool(apply and applied and not blocked),
        "backup_root": str(backup_root) if backup_root else None,
        "backup_path": str(backup_path) if backup_path else None,
        "issues": issues,
        "issue_count": len(issues) + len(blocked),
        "actions": actions,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Overlay ready Baselane native split plan rows into the local ECO GL ledger.")
    parser.add_argument("--ledger", type=Path, default=Path(os.environ.get("BASELANE_ECO_GL_LEDGER", DEFAULT_LEDGER)))
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--backup-root", type=Path)
    parser.add_argument("--rule-id", action="append", dest="rule_ids")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    rule_ids = set(args.rule_ids or DEFAULT_RULE_IDS)
    report = build_report(args.ledger, args.plan, rule_ids, args.apply, args.backup_root)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report.get(key) for key in ["status", "mutation_mode", "record_count", "applied_count", "already_overlayed_count", "blocked_count", "output_written"]}, indent=2, sort_keys=True))
    return 0 if report.get("status") == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
