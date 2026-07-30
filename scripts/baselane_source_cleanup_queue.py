#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from baselane_ecogl_data_quality_autonomy import raw_no_dao_mortgage_violation_reason, stable_digest
from baselane_first_day_pm_fee_audit import is_first_day_pm_fee_row


ROOT = Path(__file__).absolute().parents[1]
DEFAULT_LEDGER = Path("/mnt/c/Users/digit/Dropbox/Projects/assetrail/ECO Systems General Ledger.csv")
DEFAULT_STATE = ROOT / "scripts" / ".baselane_native_split_apply_state.json"
DEFAULT_REPORT = ROOT / "reports" / "baselane_source_cleanup_queue.json"
DEFAULT_CSV = ROOT / "reports" / "baselane_source_cleanup_queue.csv"
DEFAULT_MD = ROOT / "reports" / "baselane_source_cleanup_queue.md"
DEFAULT_PROPERTIES: tuple[str, ...] = ()
DEFAULT_MERCHANT_NEEDLES = (
    "hospitable",
    "hulu",
    "morgan linen",
    "netflix",
    "pricelabs",
    "price labs",
    "spectrum",
    "county waste",
    "wci*county",
)
ACTION_FIELDS = [
    "id",
    "action",
    "status",
    "property",
    "date",
    "amount",
    "merchant",
    "description",
    "baselane_id",
    "parent_baselane_id",
    "rule",
    "reason",
]


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_source_index() -> Path:
    reports = ROOT / "reports"
    canonical = reports / "baselane_source_transaction_index.csv"
    if canonical.is_file():
        return canonical
    latest = reports / "baselane_source_transaction_index.latest.csv"
    if latest.is_file():
        return latest
    candidates = sorted(
        reports.glob("baselane_source_transaction_index.*.csv"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else latest


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), [{key: str(value or "") for key, value in row.items()} for row in reader]


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ACTION_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in ACTION_FIELDS})
    tmp.replace(path)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# Baselane Source Cleanup Queue",
        "",
        f"- Status: `{report['status']}`",
        f"- Scope properties: `{', '.join(report['scope_properties']) if report['scope_properties'] else 'all'}`",
        f"- Action count: `{report['action_count']}`",
        f"- Missing ID count: `{report['missing_id_count']}`",
        f"- Live mutation attempted: `{str(report['live_mutation_attempted']).lower()}`",
        f"- Ledger: `{report['ledger']}`",
        f"- Source index: `{report['source_index']}`",
        "",
        "## Action Counts",
    ]
    for key, count in report.get("action_counts", {}).items():
        lines.append(f"- `{key}`: {count}")
    if report.get("actions_bounded"):
        lines.extend(["", "## Bounded Actions"])
        for row in report["actions_bounded"]:
            lines.append(
                f"- `{row['action']}` `{row.get('baselane_id') or 'missing-id'}` "
                f"`{row['date']}` `{row['property']}` `{row['amount']}` `{row['merchant']}`: {row['reason']}"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)


def normalize(value: object) -> str:
    return " ".join(str(value or "").strip().lower().split())


def amount_key(value: object) -> str:
    raw = str(value or "").replace("$", "").replace(",", "").strip()
    try:
        return str(Decimal(raw or "0").quantize(Decimal("0.01")))
    except InvalidOperation:
        return raw


def row_key(row: dict[str, str]) -> tuple[str, ...]:
    return (
        normalize(row.get("Date")),
        amount_key(row.get("Amount")),
        normalize(row.get("Account")),
        normalize(row.get("Merchant")),
        normalize(row.get("Description")),
        normalize(row.get("Property")),
        normalize(row.get("Type")),
        normalize(row.get("Category")),
        normalize(row.get("Sub-category")),
        normalize(row.get("Notes")),
    )


def row_key_without_notes(row: dict[str, str]) -> tuple[str, ...]:
    return row_key({**row, "Notes": ""})


def scoped(row: dict[str, str], properties: set[str], merchant_needles: tuple[str, ...]) -> bool:
    if properties and normalize(row.get("Property")) not in properties:
        return False
    haystack = normalize(" ".join(str(row.get(field) or "") for field in ("Merchant", "Description", "Notes")))
    return any(needle in haystack for needle in merchant_needles)


def source_index_by_key(source_rows: list[dict[str, str]]) -> dict[tuple[str, ...], list[dict[str, str]]]:
    by_key: dict[tuple[str, ...], list[dict[str, str]]] = defaultdict(list)
    for row in source_rows:
        keys = {row_key(row), row_key_without_notes(row)}
        for key in keys:
            by_key[key].append(row)
    for rows in by_key.values():
        rows.sort(key=lambda row: int(row.get("BaselaneId") or "0"))
    return by_key


def action_id(action: str, row: dict[str, str], baselane_id: str = "", parent_id: str = "") -> str:
    return stable_digest(
        {
            "action": action,
            "date": row.get("Date"),
            "property": row.get("Property"),
            "amount": amount_key(row.get("Amount")),
            "merchant": row.get("Merchant"),
            "description": row.get("Description"),
            "baselane_id": baselane_id,
            "parent_baselane_id": parent_id,
        }
    )[:16]


def bounded_row(
    action: str,
    row: dict[str, str],
    reason: str,
    baselane_id: str = "",
    parent_id: str = "",
    rule: str = "",
) -> dict[str, str]:
    return {
        "id": action_id(action, row, baselane_id, parent_id),
        "action": action,
        "status": "ready_id_backed" if baselane_id else "review_missing_baselane_id",
        "property": row.get("Property") or "",
        "date": row.get("Date") or "",
        "amount": row.get("Amount") or "",
        "merchant": row.get("Merchant") or "",
        "description": row.get("Description") or "",
        "baselane_id": baselane_id,
        "parent_baselane_id": parent_id,
        "rule": rule,
        "reason": reason,
    }


def duplicate_actions(
    ledger_rows: list[dict[str, str]],
    source_rows: list[dict[str, str]],
    properties: set[str],
    merchant_needles: tuple[str, ...],
) -> list[dict[str, str]]:
    source_by_key = source_index_by_key(source_rows)
    groups: dict[tuple[str, ...], list[dict[str, str]]] = defaultdict(list)
    for row in ledger_rows:
        if scoped(row, properties, merchant_needles):
            groups[row_key(row)].append(row)
    actions: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for key, rows in sorted(groups.items()):
        if len(rows) < 2:
            continue
        source_matches = source_by_key.get(key) or source_by_key.get(row_key_without_notes(rows[0])) or []
        ids = [row.get("BaselaneId", "").strip() for row in source_matches if row.get("BaselaneId", "").strip()]
        delete_ids = ids[1:len(rows)] if len(ids) >= len(rows) else []
        for index, row in enumerate(rows[1:], start=1):
            baselane_id = delete_ids[index - 1] if index - 1 < len(delete_ids) else ""
            if baselane_id and baselane_id in seen_ids:
                continue
            if baselane_id:
                seen_ids.add(baselane_id)
            actions.append(
                bounded_row(
                    "delete_duplicate_split_child",
                    row,
                    "Exact duplicate shared-service split child; keep one row and remove duplicate child from Baselane source.",
                    baselane_id=baselane_id,
                )
            )
    return actions


def no_dao_mortgage_actions(
    ledger_rows: list[dict[str, str]],
    source_rows: list[dict[str, str]],
    properties: set[str],
) -> list[dict[str, str]]:
    source_by_key = source_index_by_key(source_rows)
    actions: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for row in ledger_rows:
        if properties and normalize(row.get("Property")) not in properties:
            continue
        reason = raw_no_dao_mortgage_violation_reason(row)
        if not reason:
            continue
        source_matches = source_by_key.get(row_key(row)) or source_by_key.get(row_key_without_notes(row)) or []
        baselane_id = next((item.get("BaselaneId", "").strip() for item in source_matches if item.get("BaselaneId", "").strip()), "")
        if baselane_id and baselane_id in seen_ids:
            continue
        if baselane_id:
            seen_ids.add(baselane_id)
        actions.append(
            bounded_row(
                "remove_no_dao_mortgage_source_row",
                row,
                reason,
                baselane_id=baselane_id,
            )
        )
    return actions


def first_day_pm_fee_actions(
    ledger_rows: list[dict[str, str]],
    source_rows: list[dict[str, str]],
    properties: set[str],
) -> list[dict[str, str]]:
    source_by_key = source_index_by_key(source_rows)
    actions: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for row in ledger_rows:
        if properties and normalize(row.get("Property")) not in properties:
            continue
        if not is_first_day_pm_fee_row(row, None):
            continue
        source_matches = source_by_key.get(row_key(row)) or source_by_key.get(row_key_without_notes(row)) or []
        baselane_id = next((item.get("BaselaneId", "").strip() for item in source_matches if item.get("BaselaneId", "").strip()), "")
        if baselane_id and baselane_id in seen_ids:
            continue
        if baselane_id:
            seen_ids.add(baselane_id)
        actions.append(
            bounded_row(
                "remove_first_day_pm_fee_source_row",
                row,
                "1st-day AOPS-PM-FEE source row contaminates PM-fee reporting; clear property tag so DAO cash is not double-charged.",
                baselane_id=baselane_id,
            )
        )
    return actions


def split_state_duplicate_actions(
    state_path: Path,
    properties: set[str],
    source_rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    if not properties:
        return []
    if not state_path.is_file():
        return []
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    applied = data.get("applied") if isinstance(data, dict) else {}
    if not isinstance(applied, dict):
        return []
    current_source_ids = {
        str(row.get("BaselaneId") or "").strip()
        for row in source_rows
        if str(row.get("BaselaneId") or "").strip()
        and normalize(row.get("IsDeleted")) not in {"true", "1", "yes"}
    }
    actions: list[dict[str, str]] = []
    for record in applied.values():
        if not isinstance(record, dict):
            continue
        children = ((record.get("response") or {}).get("children") or [])
        if not isinstance(children, list):
            continue
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for child in children:
            if not isinstance(child, dict):
                continue
            merchant = str(child.get("merchantName") or "")
            property_match = any(normalize(prop) in normalize(merchant) for prop in properties)
            if not property_match:
                continue
            groups[(amount_key(child.get("amount")), normalize(merchant), str(child.get("tagId") or ""))].append(child)
        for child_group in groups.values():
            if len(child_group) < 2:
                continue
            for child in child_group[1:]:
                child_id = str(child.get("id") or "").strip()
                if child_id not in current_source_ids:
                    continue
                row = {
                    "Date": "",
                    "Property": next((prop for prop in properties if normalize(prop) in normalize(child.get("merchantName"))), ""),
                    "Amount": str(child.get("amount") or ""),
                    "Merchant": str(child.get("merchantName") or ""),
                    "Description": "",
                }
                actions.append(
                    bounded_row(
                        "delete_duplicate_split_child",
                        row,
                        "Native split response returned duplicate child rows for one property/amount/tag.",
                        baselane_id=child_id,
                        parent_id=str(record.get("baselane_id") or (record.get("response") or {}).get("parentId") or ""),
                        rule=str(record.get("rule") or ""),
                    )
                )
    return actions


def unique_actions(actions: list[dict[str, str]]) -> list[dict[str, str]]:
    by_key: dict[tuple[str, str, str], dict[str, str]] = {}
    for action in actions:
        baselane_id = action.get("baselane_id", "")
        key = (action.get("action", ""), baselane_id, "" if baselane_id else action.get("id", ""))
        if key not in by_key:
            by_key[key] = action
    return list(by_key.values())


def build_report(
    ledger: Path,
    source_index: Path,
    state: Path,
    properties: tuple[str, ...] = DEFAULT_PROPERTIES,
    merchant_needles: tuple[str, ...] = DEFAULT_MERCHANT_NEEDLES,
    limit: int = 50,
) -> dict[str, Any]:
    issues: list[str] = []
    if not ledger.is_file():
        issues.append(f"missing_ledger:{ledger}")
    if not source_index.is_file():
        issues.append(f"missing_source_index:{source_index}")
    if issues:
        return {
            "generated_at": iso_z(),
            "status": "review",
            "issues": issues,
            "issue_count": len(issues),
            "ledger": str(ledger),
            "source_index": str(source_index),
            "state": str(state),
            "live_mutation_attempted": False,
        }
    _, ledger_rows = read_csv(ledger)
    _, source_rows = read_csv(source_index)
    property_set = {normalize(prop) for prop in properties}
    actions = unique_actions(
        duplicate_actions(ledger_rows, source_rows, property_set, merchant_needles)
        + no_dao_mortgage_actions(ledger_rows, source_rows, property_set)
        + first_day_pm_fee_actions(ledger_rows, source_rows, property_set)
        + split_state_duplicate_actions(state, property_set, source_rows)
    )
    action_counts = Counter(row["action"] for row in actions)
    status_counts = Counter(row["status"] for row in actions)
    missing_id_count = status_counts.get("review_missing_baselane_id", 0)
    return {
        "generated_at": iso_z(),
        "status": "review" if missing_id_count else "ready",
        "ledger": str(ledger),
        "source_index": str(source_index),
        "state": str(state),
        "scope_properties": list(properties),
        "merchant_needles": list(merchant_needles),
        "ledger_row_count": len(ledger_rows),
        "source_index_row_count": len(source_rows),
        "action_count": len(actions),
        "action_counts": dict(sorted(action_counts.items())),
        "status_counts": dict(sorted(status_counts.items())),
        "missing_id_count": missing_id_count,
        "id_backed_action_count": len(actions) - missing_id_count,
        "live_mutation_attempted": False,
        "safe_to_apply_automatically": False,
        "policy": "Read-only source cleanup queue. Live Baselane mutation requires an explicit deletion/unsplit executor and current source-index verification.",
        "actions": actions,
        "actions_bounded": actions[:limit],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build an ID-backed Baselane source cleanup queue for duplicate split and invalid no-DAO mortgage rows.")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--source-index", type=Path, default=default_source_index())
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--property", action="append", dest="properties", default=None)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MD)
    args = parser.parse_args()
    report = build_report(
        ledger=args.ledger,
        source_index=args.source_index,
        state=args.state,
        properties=tuple(args.properties or DEFAULT_PROPERTIES),
    )
    write_json(args.report, report)
    write_csv(args.csv, report.get("actions") or [])
    write_markdown(args.markdown, report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "action_count": report.get("action_count", 0),
                "missing_id_count": report.get("missing_id_count", 0),
                "report": str(args.report),
                "csv": str(args.csv),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["status"] in {"ready", "review"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
