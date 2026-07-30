#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from baselane_web3_reconciliation_apply import (
    event_marker,
    note_text,
    query_transactions,
    run_graphql,
)


ROOT = Path(__file__).absolute().parents[1]
DEFAULT_CONFIG = ROOT / "reports" / "baselane_web3_reconciliation_events.20260729.all-signed.candidate.json"
DEFAULT_REPORT = ROOT / "reports" / "baselane_fix_yhome_earldao_fbo_legs.json"
ERRONEOUS_LEGS = {
    "earldao_to_yhome_out",
    "earldao_to_yhome_in",
    "earldao_to_property_out",
    "earldao_to_property_in",
    # Historical 1456 config used the property slug instead of "property".
    "earldao_to_1456_out",
    "earldao_to_1456_in",
}


def digest(rows: list[dict[str, Any]]) -> str:
    stable = [
        {
            "id": str(row["id"]),
            "marker": row["marker"],
            "amount": round(float(row["amount"]), 2),
            "propertyId": str(row.get("propertyId") or ""),
        }
        for row in rows
    ]
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def intended_removed_markers(config: dict[str, Any], event_id: str | None) -> dict[str, dict[str, Any]]:
    targets: dict[str, dict[str, Any]] = {}
    for event in config.get("events") or []:
        if event_id and event.get("event_id") != event_id:
            continue
        rows = event.get("ledger_rows") or []
        # Remove only the explicit second-pair legs. Some events contain adopted
        # legacy components after that pair, so positional selection is unsafe.
        for row in rows:
            if str(row.get("leg")) not in ERRONEOUS_LEGS:
                continue
            marker = event_marker(str(event["event_id"]), str(row["leg"]))
            targets[marker] = {
                "marker": marker,
                "amount": round(float(row["amount"]), 2),
                "propertyId": str(row["property_id"]),
                "event_id": str(event["event_id"]),
                "leg": str(row["leg"]),
            }
    return targets


def delete_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    mutation = """
    mutation UpdateTransaction($input: [UpdateTransaction!]) {
      updateTransactions(input: $input) {
        id isDeleted amount propertyId note
      }
    }
    """
    result = run_graphql(
        {
            "operationName": "UpdateTransaction",
            "variables": {
                "input": [
                    {"id": str(row["id"]), "isDeleted": True, "isReviewedByUser": True}
                    for row in rows
                ]
            },
            "query": mutation,
        }
    )["data"]["updateTransactions"]
    returned = {str(row["id"]): row for row in result}
    expected = {str(row["id"]) for row in rows}
    if set(returned) != expected or not all(row.get("isDeleted") for row in returned.values()):
        raise RuntimeError("Baselane did not confirm every bounded deletion")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--event-id")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-digest")
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    targets = intended_removed_markers(config, args.event_id)
    live_rows = query_transactions("WEB3-WEB2-RECON", page_limit=500)
    active: list[dict[str, Any]] = []
    issues: list[str] = []
    for marker, expected in targets.items():
        matches = [row for row in live_rows if marker in note_text(row.get("note"))]
        if len(matches) > 1:
            issues.append(f"duplicate:{marker}:{[row.get('id') for row in matches]}")
            continue
        if not matches:
            continue
        row = matches[0]
        if round(float(row.get("amount") or 0), 2) != expected["amount"]:
            issues.append(f"amount_changed:{marker}:{row.get('amount')}")
            continue
        if str(row.get("propertyId") or "") != expected["propertyId"]:
            issues.append(f"property_changed:{marker}:{row.get('propertyId')}")
            continue
        active.append({**row, "marker": marker})

    current_digest = digest(active)
    if args.apply and args.confirm_digest != current_digest:
        raise RuntimeError(f"confirm digest required; current digest is {current_digest}")
    applied = delete_rows(active) if args.apply and not issues else []

    remaining_rows = query_transactions("WEB3-WEB2-RECON", page_limit=500) if args.apply else live_rows
    remaining_markers = {
        marker
        for marker in targets
        if any(marker in note_text(row.get("note")) for row in remaining_rows)
    }
    status = "blocked" if issues else ("ok" if not remaining_markers or not args.apply else "blocked")
    report = {
        "status": status,
        "mode": "apply" if args.apply else "preview",
        "event_id": args.event_id,
        "target_marker_count": len(targets),
        "active_delete_count": len(active),
        "active_delete_total": round(sum(float(row["amount"]) for row in active), 2),
        "digest": current_digest,
        "issues": issues,
        "targets": [
            {
                "id": str(row["id"]),
                "marker": row["marker"],
                "amount": round(float(row["amount"]), 2),
                "propertyId": str(row.get("propertyId") or ""),
            }
            for row in active
        ],
        "applied_ids": [str(row["id"]) for row in applied],
        "remaining_target_markers": sorted(remaining_markers),
        "cash_movement_created": False,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in (
        "status", "mode", "active_delete_count", "active_delete_total",
        "digest", "applied_ids", "cash_movement_created"
    )}, indent=2))
    return 0 if status == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
