#!/usr/bin/env python3
"""Remove one verified historical duplicate PM accrual from Baselane."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import baselane_apply_monthly_accruals_live as live


REPORT = ROOT / "reports" / "baselane_exact_pm_accrual_duplicate_live.json"
CANONICAL = {
    "id": "254362455",
    "amount": -2618.46,
    "date": "2025-07-01",
    "merchantName": "PM Fees 25% June 2025",
    "propertyId": "31499",
    "tagId": "80",
    "note": "PM Fees 25% June 2025 Due and Payable to ECO Systems",
}
REDUNDANT = {**CANONICAL, "id": "254361994", "tagId": "18"}


def normalized_note(value: Any) -> str:
    return " ".join(live.note_text(value).split())


def row_issues(row: dict[str, Any] | None, expected: dict[str, Any]) -> list[str]:
    row_id = expected["id"]
    if row is None:
        return [f"missing:{row_id}"]
    actual = {
        "amount": round(float(row.get("amount") or 0), 2),
        "date": str(row.get("date") or "")[:10],
        "merchantName": str(row.get("merchantName") or "").strip(),
        "propertyId": str(row.get("propertyId") or ""),
        "tagId": str(row.get("tagId") or ""),
        "note": normalized_note(row.get("note")),
    }
    issues = [
        f"{key}_mismatch:{row_id}:{actual[key]!r}"
        for key in actual
        if actual[key] != expected[key]
    ]
    if not bool(row.get("isManual")):
        issues.append(f"not_manual:{row_id}")
    return issues


def build_plan(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    by_id = {str(row.get("id") or ""): row for row in rows}
    canonical_issues = row_issues(by_id.get(CANONICAL["id"]), CANONICAL)
    redundant = by_id.get(REDUNDANT["id"])
    if redundant is None and not canonical_issues:
        return [], []
    issues = canonical_issues + row_issues(redundant, REDUNDANT)
    if issues:
        return [], issues
    return [
        {
            "action": "delete_exact_duplicate",
            "delete_id": REDUNDANT["id"],
            "preserve_id": CANONICAL["id"],
            "property_id": CANONICAL["propertyId"],
            "date": CANONICAL["date"],
            "amount": f"{CANONICAL['amount']:.2f}",
            "merchant": CANONICAL["merchantName"],
            "note": CANONICAL["note"],
            "delete_tag_id": REDUNDANT["tagId"],
            "preserve_tag_id": CANONICAL["tagId"],
        }
    ], []


def action_digest(actions: list[dict[str, Any]]) -> str:
    payload = json.dumps(actions, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def delete_redundant() -> list[dict[str, Any]]:
    result = live.run_graphql(
        {
            "operationName": "UpdateTransactions",
            "variables": {
                "input": [
                    {
                        "id": REDUNDANT["id"],
                        "isDeleted": True,
                        "isReviewedByUser": True,
                    }
                ]
            },
            "query": """
              mutation UpdateTransactions($input: [UpdateTransaction!]) {
                updateTransactions(input: $input) { id isDeleted }
              }
            """,
        }
    )
    return result["data"]["updateTransactions"]


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--require-action-digest")
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args(argv)
    if args.apply and not args.require_action_digest:
        parser.error("--apply requires --require-action-digest")

    mutation_results: list[dict[str, Any]] = []
    verification_failures: list[str] = []
    with live.exclusive_pipeline_lock(args.apply) as lock_acquired:
        if not lock_acquired:
            raise SystemExit("Baselane source pipeline lock is held by another process")
        rows = live.query_transactions(CANONICAL["merchantName"])
        actions, issues = build_plan(rows)
        digest = action_digest(actions)
        if args.apply and digest != args.require_action_digest:
            raise SystemExit(
                f"action digest changed: expected {args.require_action_digest}, current {digest}"
            )
        if args.apply and issues:
            raise SystemExit(f"live verification blocked mutation: {issues}")
        if args.apply and actions:
            mutation_results = delete_redundant()
            verified_rows = live.query_transactions(CANONICAL["merchantName"])
            verified_by_id = {
                str(row.get("id") or ""): row for row in verified_rows
            }
            if REDUNDANT["id"] in verified_by_id:
                verification_failures.append(
                    f"redundant_row_still_active:{REDUNDANT['id']}"
                )
            verification_failures.extend(
                row_issues(verified_by_id.get(CANONICAL["id"]), CANONICAL)
            )

    status = "ok" if not issues and not verification_failures else "blocked"
    report = {
        "status": status,
        "mode": "apply" if args.apply else "preview",
        "action_digest": digest,
        "action_count": len(actions),
        "actions": actions,
        "issues": issues,
        "mutation_results": mutation_results,
        "verification_failures": verification_failures,
    }
    write_report(args.report, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if status == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
