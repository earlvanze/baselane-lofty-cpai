#!/usr/bin/env python3
"""Remove the obsolete positive Madison PM settlement reversals from Baselane.

The rows below were the old cash-settlement workaround. They reverse the DAO
expense on the income statement and are superseded by paired DAO-expense/ECO-
revenue accrual rows. This command is deliberately bounded to exact live IDs,
amounts, properties, and markers and requires its dry-run digest to apply.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from scripts import baselane_apply_monthly_accruals_live as live


ROOT = Path(__file__).resolve().parents[1]
TARGETS = [
    ("314061616", 2328.96, "31499", "88 Madison Ave", "2025-08"),
    ("314061615", 1493.52, "63162", "86 Madison Ave", "2025-08"),
    ("314061617", 1526.08, "31525", "90 Madison Ave", "2025-08"),
    ("314061667", 1338.63, "63162", "86 Madison Ave", "2025-10"),
    ("314061668", 2488.66, "31499", "88 Madison Ave", "2025-10"),
    ("314061669", 1924.07, "31525", "90 Madison Ave", "2025-10"),
    ("314061704", 1147.14, "63162", "86 Madison Ave", "2025-11"),
    ("314061736", 1147.14, "63162", "86 Madison Ave", "2025-12"),
    ("314061744", 1871.20, "31499", "88 Madison Ave", "2025-12"),
    ("314061774", 1230.29, "31525", "90 Madison Ave", "2025-12"),
    ("314061842", 1111.48, "63162", "86 Madison Ave", "2026-01"),
    ("314061920", 863.24, "63162", "86 Madison Ave", "2026-02"),
    ("314061925", 1915.11, "31499", "88 Madison Ave", "2026-02"),
    ("314061928", 1405.89, "31525", "90 Madison Ave", "2026-02"),
    ("314061944", 1287.36, "31525", "90 Madison Ave", "2026-03"),
    ("314061942", 2000.00, "31499", "88 Madison Ave", "2026-03"),
]


def action_digest(targets: list[dict[str, Any]]) -> str:
    bounded = [
        {
            "id": row["id"],
            "amount": row["amount"],
            "property_id": row["property_id"],
            "marker_key": row["marker_key"],
        }
        for row in targets
    ]
    return hashlib.sha256(
        json.dumps(bounded, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def expected_targets() -> list[dict[str, Any]]:
    return [
        {
            "id": row_id,
            "amount": amount,
            "property_id": property_id,
            "marker_key": (
                f"AOPS-PNL-ACCRUAL|pm_settlement|{property_name}|{month}"
            ),
        }
        for row_id, amount, property_id, property_name, month in TARGETS
    ]


def delete_rows(ids: list[str]) -> list[dict[str, Any]]:
    return live.run_graphql(
        {
            "operationName": "UpdateTransactions",
            "variables": {
                "input": [
                    {"id": row_id, "isDeleted": True, "isReviewedByUser": True}
                    for row_id in ids
                ]
            },
            "query": """
              mutation UpdateTransactions($input: [UpdateTransaction!]) {
                updateTransactions(input: $input) { id isDeleted }
              }
            """,
        }
    )["data"]["updateTransactions"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "reports" / "madison_legacy_pm_settlements_live.json",
    )
    parser.add_argument("--require-action-digest")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    targets = expected_targets()
    digest = action_digest(targets)
    if args.apply and args.require_action_digest != digest:
        raise RuntimeError(
            f"apply requires the current action digest; current digest is {digest}"
        )

    live_rows = live.query_transactions("AOPS-PNL-ACCRUAL|pm_settlement")
    by_id = {str(row.get("id")): row for row in live_rows}
    issues: list[str] = []
    actions: list[dict[str, Any]] = []
    for target in targets:
        row = by_id.get(target["id"])
        if not row:
            issues.append(f"missing_live_target:{target['id']}")
            continue
        marker = live.parse_marker(live.note_text(row.get("note")))
        actual_marker = marker["key"] if marker else ""
        if round(float(row.get("amount") or 0), 2) != target["amount"]:
            issues.append(f"amount_mismatch:{target['id']}")
        if str(row.get("propertyId") or "") != target["property_id"]:
            issues.append(f"property_mismatch:{target['id']}")
        if actual_marker != target["marker_key"]:
            issues.append(f"marker_mismatch:{target['id']}:{actual_marker}")
        if not bool(row.get("isManual")):
            issues.append(f"not_manual:{target['id']}")
        actions.append(
            {
                "action": "delete",
                **target,
                "merchant_name": row.get("merchantName"),
                "note": live.note_text(row.get("note")),
            }
        )

    expected_ids = {target["id"] for target in targets}
    unexpected = sorted(set(by_id) - expected_ids)
    if unexpected:
        issues.append(f"unexpected_scoped_live_rows:{unexpected}")

    results: list[dict[str, Any]] = []
    verification: dict[str, Any] = {}
    if args.apply and not issues:
        results = delete_rows([target["id"] for target in targets])
        result_by_id = {str(row.get("id")): row for row in results}
        failed_results = [
            row_id
            for row_id in expected_ids
            if not bool(result_by_id.get(row_id, {}).get("isDeleted"))
        ]
        remaining = live.query_transactions("AOPS-PNL-ACCRUAL|pm_settlement")
        remaining_ids = sorted(
            str(row.get("id"))
            for row in remaining
            if str(row.get("id")) in expected_ids
        )
        verification = {
            "mutation_failed_ids": sorted(failed_results),
            "remaining_active_ids": remaining_ids,
        }
        if failed_results or remaining_ids:
            issues.append("post_apply_verification_failed")

    report = {
        "mode": "apply" if args.apply else "dry_run",
        "status": "ok" if not issues else "blocked",
        "action_digest": digest,
        "target_count": len(targets),
        "action_count": len(actions),
        "issues": issues,
        "actions": actions,
        "results": results,
        "verification": verification,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "mode": report["mode"],
                "target_count": report["target_count"],
                "action_digest": digest,
                "issue_count": len(issues),
            },
            indent=2,
        )
    )
    return 0 if not issues else 2


if __name__ == "__main__":
    raise SystemExit(main())
