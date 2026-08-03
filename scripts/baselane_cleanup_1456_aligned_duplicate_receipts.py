#!/usr/bin/env python3
"""Delete two verified duplicate 1456 Aligned rent-detail rows in Baselane."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

try:
    from .baselane_apply_alcott_accruals_live import run_graphql
except ImportError:
    from baselane_apply_alcott_accruals_live import run_graphql


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT = ROOT / "reports" / "baselane_1456_aligned_duplicate_receipts_cleanup.json"
DEFAULT_SOURCE_STATEMENT = Path(
    "/mnt/c/Users/digit/Dropbox/Real Estate/OH/1456 W 85th St, Cleveland, OH 44102/"
    "Public/07 - P&L & Owner Statements/"
    "Owner Statement - Jun 13, 2026 to Jul 10, 2026 - 1456 W 85th St, Cleveland, OH 44102.pdf"
)
SOURCE_STATEMENT_SHA256 = "9ccd2a6b747b8fd8033fe5b8f44f32aad1521fcffc090ce9daef072a3e89922f"
PREFIX = "aligned-1456-w-85th-st-cleveland-oh-4410"
PROPERTY_ID = "81428"
TAG_ID = "136"
SOURCE_BASENAME = "0023-owner-statement-jun-13-2026-to-jul-10-2026-1456-w-85th-st-cleveland-oh-44102.pdf"

KEEP_TARGETS = (
    {
        "id": "323471159",
        "key": f"{PREFIX}-5eb3d63bd472448e",
        "date": "2026-07-07",
        "amount": "500.00",
        "merchantName": "Angela Heath",
        "source_line": "line=49",
    },
    {
        "id": "323471160",
        "key": f"{PREFIX}-a924f47d3f95b175",
        "date": "2026-07-07",
        "amount": "435.18",
        "merchantName": "Angela Heath",
        "source_line": "line=50",
    },
)

DELETE_TARGETS = (
    {
        "id": "323516020",
        "key": f"{PREFIX}-e42dbd44d9f1614c",
        "date": "2026-07-07",
        "amount": "500.00",
        "merchantName": "Angela Heath Receipt",
        "source_line": "line=51",
    },
    {
        "id": "323516025",
        "key": f"{PREFIX}-d5fd96da5b6a594b",
        "date": "2026-07-07",
        "amount": "435.18",
        "merchantName": "Angela Heath Receipt",
        "source_line": "line=52",
    },
)


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def note_text(value: Any) -> str:
    return str(value.get("text") or "") if isinstance(value, dict) else str(value or "")


def cents(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def query_transactions(*, is_deleted: bool) -> list[dict[str, Any]]:
    rows_by_id: dict[str, dict[str, Any]] = {}
    for target in KEEP_TARGETS + DELETE_TARGETS:
        page = 1
        while True:
            payload = {
                "operationName": "Transactions",
                "variables": {
                    "input": {
                        "sort": {"direction": "DESC", "field": "date"},
                        "filter": {"search": target["key"], "isHidden": False, "isDeleted": is_deleted},
                        "page": page,
                        "pageLimit": 25,
                    }
                },
                "query": """
                query Transactions($input: SortsAndFilters) {
                  transactions(input: $input) {
                    total
                    data {
                      id amount date merchantName bankAccountId propertyId tagId note
                      isManual hidden isDeleted
                    }
                  }
                }
                """,
            }
            result = run_graphql(payload)["data"]["transactions"]
            batch = result.get("data") or []
            for row in batch:
                if target["key"] in note_text(row.get("note")):
                    rows_by_id[str(row.get("id") or "")] = row
            total = int(result.get("total") or 0)
            if not batch or page * 25 >= total:
                break
            page += 1
            if page > 20:
                raise RuntimeError(f"exact-key query exceeded 20 pages: {target['key']}")
    return list(rows_by_id.values())


def update_transactions(inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload = {
        "operationName": "UpdateTransaction",
        "variables": {"input": inputs},
        "query": """
        mutation UpdateTransaction($input: [UpdateTransaction!]) {
          updateTransactions(input: $input) {
            id amount date merchantName bankAccountId propertyId tagId note
            isManual hidden isDeleted
          }
        }
        """,
    }
    return run_graphql(payload)["data"]["updateTransactions"]


def identity_issues(row: dict[str, Any], target: dict[str, str], *, deleted: bool) -> list[str]:
    note = note_text(row.get("note"))
    checks = {
        "propertyId": str(row.get("propertyId") or "") == PROPERTY_ID,
        "tagId": str(row.get("tagId") or "") == TAG_ID,
        "date": str(row.get("date") or "") == target["date"],
        "amount": cents(row.get("amount")) == cents(target["amount"]),
        "merchantName": str(row.get("merchantName") or "") == target["merchantName"],
        "isManual": bool(row.get("isManual")),
        "isDeleted": bool(row.get("isDeleted")) is deleted,
        "bankAccountId": row.get("bankAccountId") in (None, ""),
        "key": target["key"] in note,
        "source": SOURCE_BASENAME in note,
        "source_line": target["source_line"] in note,
        "rent_detail": "| Rent or tenant receipt |" in note,
        "no_cash_movement": "accounting/manual detail only, no ECO bank transfer" in note,
    }
    return [name for name, ok in checks.items() if not ok]


def assess_live_state(
    active_rows: list[dict[str, Any]],
    deleted_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    active_by_id = {str(row.get("id") or ""): row for row in active_rows}
    deleted_by_id = {str(row.get("id") or ""): row for row in deleted_rows}
    issues: list[dict[str, Any]] = []
    active_delete_targets: list[dict[str, Any]] = []
    already_deleted_targets: list[dict[str, Any]] = []

    for target in KEEP_TARGETS:
        row = active_by_id.get(target["id"])
        if row is None:
            issues.append({"id": target["id"], "role": "keep", "issue": "active_row_missing"})
            continue
        failed = identity_issues(row, target, deleted=False)
        if failed:
            issues.append({"id": target["id"], "role": "keep", "issue": "identity_mismatch", "failed": failed})

    for target in DELETE_TARGETS:
        active = active_by_id.get(target["id"])
        deleted = deleted_by_id.get(target["id"])
        if active is not None and deleted is not None:
            issues.append({"id": target["id"], "role": "delete", "issue": "present_active_and_deleted"})
            continue
        if active is not None:
            failed = identity_issues(active, target, deleted=False)
            if failed:
                issues.append({"id": target["id"], "role": "delete", "issue": "identity_mismatch", "failed": failed})
            else:
                active_delete_targets.append(active)
            continue
        if deleted is not None:
            failed = identity_issues(deleted, target, deleted=True)
            if failed:
                issues.append({"id": target["id"], "role": "delete", "issue": "deleted_identity_mismatch", "failed": failed})
            else:
                already_deleted_targets.append(deleted)
            continue
        issues.append({"id": target["id"], "role": "delete", "issue": "row_missing"})

    if issues:
        status = "blocked"
    elif len(already_deleted_targets) == len(DELETE_TARGETS):
        status = "already_applied"
    else:
        status = "ready"
    return {
        "status": status,
        "issues": issues,
        "active_delete_targets": active_delete_targets,
        "already_deleted_targets": already_deleted_targets,
    }


def payload_digest() -> str:
    payload = {
        "job": "baselane-1456-aligned-duplicate-receipts-cleanup",
        "propertyId": PROPERTY_ID,
        "tagId": TAG_ID,
        "source_statement_sha256": SOURCE_STATEMENT_SHA256,
        "keep_targets": KEEP_TARGETS,
        "delete_targets": DELETE_TARGETS,
        "mutation": [
            {"id": target["id"], "isDeleted": True, "isReviewedByUser": True}
            for target in DELETE_TARGETS
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-digest", default="")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--source-statement", type=Path, default=DEFAULT_SOURCE_STATEMENT)
    args = parser.parse_args()

    digest = payload_digest()
    statement_hash = file_sha256(args.source_statement)
    source_issue = None if statement_hash == SOURCE_STATEMENT_SHA256 else "source_statement_hash_mismatch_or_missing"
    active_rows = query_transactions(is_deleted=False)
    deleted_rows = query_transactions(is_deleted=True)
    assessment = assess_live_state(active_rows, deleted_rows)
    issues = list(assessment["issues"])
    if source_issue:
        issues.append({"issue": source_issue, "actual_sha256": statement_hash})

    status = "blocked" if issues else assessment["status"]
    report: dict[str, Any] = {
        "job": "baselane-1456-aligned-duplicate-receipts-cleanup",
        "generated_at": iso_z(),
        "status": status,
        "apply": bool(args.apply),
        "payload_digest": digest,
        "property_id": PROPERTY_ID,
        "source_statement": str(args.source_statement),
        "source_statement_sha256": statement_hash,
        "source_statement_expected_sha256": SOURCE_STATEMENT_SHA256,
        "statement_july_rent_total": "1523.57",
        "duplicate_amount_total": "935.18",
        "correct_pm_fee_at_10_percent": "152.36",
        "keep_targets": list(KEEP_TARGETS),
        "delete_targets": list(DELETE_TARGETS),
        "active_delete_target_ids": [str(row.get("id")) for row in assessment["active_delete_targets"]],
        "already_deleted_target_ids": [str(row.get("id")) for row in assessment["already_deleted_targets"]],
        "issues": issues,
        "mutation_results": [],
    }

    if args.apply and status == "ready":
        if args.expected_digest != digest:
            report["status"] = "blocked_digest_mismatch"
            report["expected_digest_argument"] = args.expected_digest
            write_report(args.report, report)
            print(json.dumps(report, indent=2, sort_keys=True))
            return 2
        report["status"] = "applying"
        write_report(args.report, report)
        inputs = [
            {"id": target["id"], "isDeleted": True, "isReviewedByUser": True}
            for target in DELETE_TARGETS
        ]
        results = update_transactions(inputs)
        report["mutation_results"] = results
        returned = {str(row.get("id") or ""): bool(row.get("isDeleted")) for row in results}
        expected_ids = {target["id"] for target in DELETE_TARGETS}
        if set(returned) != expected_ids or not all(returned.values()):
            report["status"] = "post_verify_failed"
            report["issues"].append({"issue": "mutation_result_mismatch", "returned": returned})
        else:
            post = assess_live_state(
                query_transactions(is_deleted=False),
                query_transactions(is_deleted=True),
            )
            report["post_readback"] = post
            report["status"] = "applied" if post["status"] == "already_applied" else "post_verify_failed"
            if post["issues"]:
                report["issues"].extend(post["issues"])
    elif args.apply and status == "already_applied":
        report["status"] = "already_applied"

    report["completed_at"] = iso_z()
    write_report(args.report, report)
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "status",
                    "payload_digest",
                    "active_delete_target_ids",
                    "already_deleted_target_ids",
                    "statement_july_rent_total",
                    "duplicate_amount_total",
                    "correct_pm_fee_at_10_percent",
                    "issues",
                )
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["status"] in {"ready", "applied", "already_applied"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
