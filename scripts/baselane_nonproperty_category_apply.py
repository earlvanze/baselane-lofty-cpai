#!/usr/bin/env python3
"""Guardedly apply a reviewed Non-Property Expense normalization plan."""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import baselane_full_property_coverage_apply as coverage


DEFAULT_PLAN = coverage.DROPBOX_REPORTS / "baselane_nonproperty_category_plan.json"
DEFAULT_REPORT = coverage.DROPBOX_REPORTS / "baselane_nonproperty_category_apply.json"
APPLY_ENV = "BASELANE_NONPROPERTY_CATEGORY_APPLY"


def classify(
    plan: dict[str, Any],
    live_rows: list[dict[str, Any]],
    property_ids: dict[str, list[str]],
    tag_ids: dict[str, list[str]],
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in live_rows:
        groups[coverage.live_key(row)].append(row)
    nonproperty_ids = set(tag_ids.get(coverage.norm("Non-Property Expense")) or [])

    records: list[dict[str, Any]] = []
    for planned in plan.get("rows") or []:
        record = dict(planned)
        record.update(
            {
                "baselane_id": "",
                "current_property_id": "",
                "current_tag_id": "",
                "target_property_id": "",
                "target_tag_id": "",
                "apply_status": "",
                "apply_reason": "",
            }
        )
        matches = sorted(
            groups.get(coverage.plan_key(planned), []),
            key=lambda row: str(row.get("id") or ""),
            reverse=True,
        )
        expected = int(planned.get("source_key_cardinality") or 0)
        occurrence = int(planned.get("occurrence") or 0)
        if len(matches) != expected:
            record["apply_status"] = "blocked_key_cardinality_changed"
            record["apply_reason"] = f"live={len(matches)} source={expected}"
            records.append(record)
            continue
        if occurrence < 1 or occurrence > len(matches):
            record["apply_status"] = "blocked_occurrence_missing"
            records.append(record)
            continue
        live = matches[occurrence - 1]
        record["baselane_id"] = str(live.get("id") or "")
        record["current_property_id"] = str(live.get("propertyId") or "")
        record["current_tag_id"] = str(live.get("tagId") or "")
        record["parent_id"] = str(live.get("parentId") or "")

        targets = list(
            dict.fromkeys(property_ids.get(coverage.norm(planned["target_property"])) or [])
        )
        tags = list(
            dict.fromkeys(tag_ids.get(coverage.norm(planned["target_category"])) or [])
        )
        if len(targets) != 1:
            record["apply_status"] = "blocked_property_metadata"
            record["apply_reason"] = f"property ID matches={len(targets)}"
        elif len(tags) != 1:
            record["apply_status"] = "blocked_tag_metadata"
            record["apply_reason"] = f"tag ID matches={len(tags)}"
        else:
            record["target_property_id"] = targets[0]
            record["target_tag_id"] = tags[0]
            current_property = str(live.get("propertyId") or "")
            current_tag = str(live.get("tagId") or "")
            if live.get("pending"):
                record["apply_status"] = "blocked_pending"
            elif live.get("isSplit"):
                record["apply_status"] = "blocked_split_parent"
            elif live.get("hidden") or live.get("isDeleted"):
                record["apply_status"] = "blocked_inactive"
            elif (
                current_property == record["target_property_id"]
                and current_tag == record["target_tag_id"]
            ):
                record["apply_status"] = "already_applied"
            elif current_tag not in nonproperty_ids:
                record["apply_status"] = "blocked_current_category_changed"
                record["apply_reason"] = f"live tagId={current_tag}"
            elif (
                planned.get("current_property") not in {"", "Personal"}
                and current_property != record["target_property_id"]
            ):
                record["apply_status"] = "blocked_current_property_changed"
                record["apply_reason"] = f"live propertyId={current_property}"
            else:
                record["apply_status"] = "ready"
        records.append(record)
    return records


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--require-plan-digest")
    parser.add_argument("--batch-size", type=int, default=25)
    args = parser.parse_args()
    if args.batch_size < 1 or args.batch_size > 50:
        raise SystemExit("--batch-size must be 1..50")

    plan = json.loads(args.plan.read_text())
    digest = coverage.plan_digest(plan)
    if args.apply and os.environ.get(APPLY_ENV) != "1":
        raise SystemExit(f"--apply requires {APPLY_ENV}=1")
    if args.apply and args.require_plan_digest != digest:
        raise SystemExit("--apply requires the exact current --require-plan-digest")

    with coverage.exclusive_lock() as acquired:
        if not acquired:
            report = {
                "status": "locked",
                "generated_at": coverage.iso_z(),
                "mode": "apply" if args.apply else "dry_run",
                "plan_digest": digest,
                "lock": str(coverage.LOCK_PATH),
            }
            write_report(args.report, report)
            print(json.dumps(report, indent=2))
            return 75

        property_ids, tag_ids, _ = coverage.fetch_metadata()
        live_rows, live_total = coverage.fetch_all_transactions()
        records = classify(plan, live_rows, property_ids, tag_ids)
        ready = sum(row["apply_status"] == "ready" for row in records)
        already = sum(row["apply_status"] == "already_applied" for row in records)
        applied = failed = 0
        if args.apply:
            applied, failed = coverage.execute(records, args.batch_size)
        blocked = sum(row["apply_status"].startswith("blocked") for row in records)
        report = {
            "status": "ok" if blocked == 0 and failed == 0 else "review",
            "generated_at": coverage.iso_z(),
            "mode": "apply" if args.apply else "dry_run",
            "plan": str(args.plan),
            "plan_digest": digest,
            "live_transaction_count": live_total,
            "plan_row_count": len(records),
            "ready_count": ready,
            "already_applied_count": already,
            "applied_verified_count": applied,
            "blocked_count": blocked,
            "failed_count": failed,
            "status_counts": dict(Counter(row["apply_status"] for row in records)),
            "records": records,
        }
        write_report(args.report, report)
        print(
            json.dumps(
                {key: value for key, value in report.items() if key != "records"},
                indent=2,
            )
        )
        return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
