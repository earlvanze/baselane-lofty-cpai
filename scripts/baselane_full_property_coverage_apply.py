#!/usr/bin/env python3
"""Guardedly apply the full blank-property coverage plan to live Baselane."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator


ROOT = Path("/home/digit/.openclaw/workspace")
DROPBOX_REPORTS = Path(
    "/mnt/c/Users/digit/Dropbox/Projects/cyber-gateway/config/openclaw/workspace/reports"
)
DEFAULT_PLAN = DROPBOX_REPORTS / "baselane_full_property_coverage_plan.json"
DEFAULT_REPORT = DROPBOX_REPORTS / "baselane_full_property_coverage_apply.json"
BRIDGE = ROOT / "scripts" / "baselane_graphql_via_cdp.js"
LOCK_PATH = Path(
    os.environ.get("BASELANE_SOURCE_PIPELINE_LOCK", "/tmp/baselane-source-pipeline.lock")
)
APPLY_ENV = "BASELANE_FULL_PROPERTY_COVERAGE_APPLY"

sys.path.insert(0, str(ROOT / "skills" / "baselane-mcp" / "src"))
from baselane_mcp.transfers import run_graphql_via_cdp  # noqa: E402


TRANSACTIONS_QUERY = """
query Transactions($input: SortsAndFilters) {
  transactions(input: $input) {
    total
    data {
      id amount date merchantName description name pending propertyId tagId
      isSplit parentId hidden isDeleted
    }
  }
}
""".strip()

VERIFY_FIELDS = (
    "id amount date merchantName description name pending propertyId tagId "
    "isSplit parentId hidden isDeleted"
)


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def norm(value: object) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", str(value or "").upper()).strip()


def amount_key(value: object) -> str:
    try:
        return f"{Decimal(str(value or '0').replace(',', '').strip()):.2f}"
    except InvalidOperation:
        return str(value or "").strip()


def date_key(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    for fmt in ("%Y-%m-%d", "%B %d, %Y"):
        try:
            return datetime.strptime(raw[:10] if fmt == "%Y-%m-%d" else raw, fmt).date().isoformat()
        except ValueError:
            continue
    return raw


def live_description(row: dict[str, Any]) -> str:
    return str(
        row.get("description")
        or row.get("name")
        or row.get("merchantName")
        or ""
    )


def live_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        date_key(row.get("date")),
        amount_key(row.get("amount")),
        norm(row.get("merchantName")),
        norm(live_description(row)),
    )


def plan_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        date_key(row.get("date")),
        amount_key(row.get("amount")),
        norm(row.get("merchant")),
        norm(row.get("description")),
    )


def plan_digest(payload: dict[str, Any]) -> str:
    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    material = [
        {
            "fingerprint": row.get("fingerprint"),
            "occurrence": row.get("occurrence"),
            "source_key_cardinality": row.get("source_key_cardinality"),
            "target_property": row.get("target_property"),
            "target_category": row.get("target_category"),
        }
        for row in rows
        if isinstance(row, dict)
    ]
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@contextmanager
def exclusive_lock(path: Path = LOCK_PATH) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def graphql(payload: dict[str, Any]) -> dict[str, Any]:
    return run_graphql_via_cdp(
        payload,
        bridge_path=BRIDGE,
        workspace_root=ROOT,
        timeout=180,
    )


def fetch_metadata() -> tuple[dict[str, list[str]], dict[str, list[str]], dict[str, str]]:
    properties_payload = graphql(
        {
            "operationName": "PropertyList",
            "variables": {},
            "query": "query PropertyList { property { id name address } }",
        }
    )
    property_ids: dict[str, list[str]] = defaultdict(list)
    property_names: dict[str, str] = {}
    for row in (properties_payload.get("data") or {}).get("property") or []:
        row_id = str(row.get("id") or "")
        name = str(row.get("name") or row.get("address") or "").strip()
        if row_id and name:
            property_ids[norm(name)].append(row_id)
            property_names[row_id] = name

    tags_payload = graphql(
        {
            "operationName": "TagList",
            "variables": {},
            "query": (
                "query TagList { tag { type subType { id name "
                "subType { id name subType { id name } } } } }"
            ),
        }
    )
    tag_ids: dict[str, list[str]] = defaultdict(list)

    def walk(items: list[dict[str, Any]]) -> None:
        for item in items:
            item_id = str(item.get("id") or "")
            name = str(item.get("name") or "").strip()
            if item_id and name:
                tag_ids[norm(name)].append(item_id)
            children = item.get("subType")
            if isinstance(children, list):
                walk(children)

    for group in (tags_payload.get("data") or {}).get("tag") or []:
        walk(group.get("subType") or [])
    return property_ids, tag_ids, property_names


def fetch_all_transactions(page_limit: int = 500) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    total = 0
    page = 1
    while True:
        payload = graphql(
            {
                "operationName": "Transactions",
                "variables": {
                    "input": {
                        "sort": {"field": "id", "direction": "DESC"},
                        "filter": {
                            "isHidden": False,
                            "search": "",
                            "isDeleted": False,
                        },
                        "page": page,
                        "pageLimit": page_limit,
                    }
                },
                "query": TRANSACTIONS_QUERY,
            }
        )
        result = (payload.get("data") or {}).get("transactions") or {}
        batch = result.get("data") or []
        total = int(result.get("total") or total)
        if not batch:
            break
        for row in batch:
            row_id = str(row.get("id") or "")
            if not row_id or row_id in seen:
                raise RuntimeError(f"duplicate or missing transaction ID during pagination: {row_id!r}")
            seen.add(row_id)
            rows.append(row)
        if len(rows) >= total:
            break
        page += 1
    if total != len(rows):
        raise RuntimeError(f"live pagination incomplete: fetched={len(rows)} total={total}")
    return rows, total


def verify_by_ids(ids: list[str]) -> dict[str, dict[str, Any]]:
    fields = [
        f't{index}: transaction(id: {json.dumps(row_id)}) {{ {VERIFY_FIELDS} }}'
        for index, row_id in enumerate(ids)
    ]
    payload = graphql(
        {
            "operationName": "VerifyCoverageTransactions",
            "variables": {},
            "query": "query VerifyCoverageTransactions {\n" + "\n".join(fields) + "\n}",
        }
    )
    data = payload.get("data") or {}
    return {
        str(row.get("id")): row
        for row in data.values()
        if isinstance(row, dict) and row.get("id")
    }


def update_batch(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload = graphql(
        {
            "operationName": "UpdateCoverageTransactions",
            "variables": {
                "input": [
                    {
                        "id": row["baselane_id"],
                        "propertyId": row["target_property_id"],
                        "tagId": row["target_tag_id"],
                        "isReviewedByUser": True,
                    }
                    for row in rows
                ]
            },
            "query": (
                "mutation UpdateCoverageTransactions($input: [UpdateTransaction!]) { "
                "updateTransactions(input: $input) { id propertyId tagId } }"
            ),
        }
    )
    return (payload.get("data") or {}).get("updateTransactions") or []


def classify(
    plan: dict[str, Any],
    live_rows: list[dict[str, Any]],
    property_ids: dict[str, list[str]],
    tag_ids: dict[str, list[str]],
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in live_rows:
        groups[live_key(row)].append(row)

    records: list[dict[str, Any]] = []
    for planned in plan.get("rows") or []:
        record = dict(planned)
        record["baselane_id"] = ""
        record["target_tag_id"] = ""
        record["apply_status"] = ""
        record["apply_reason"] = ""
        key = plan_key(planned)
        matches = groups.get(key) or []
        expected_cardinality = int(planned.get("source_key_cardinality") or 0)
        occurrence = int(planned.get("occurrence") or 0)
        if len(matches) != expected_cardinality:
            record["apply_status"] = "blocked_key_cardinality_changed"
            record["apply_reason"] = f"live={len(matches)} source={expected_cardinality}"
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

        prop_matches = list(dict.fromkeys(property_ids.get(norm(planned.get("target_property"))) or []))
        tag_matches = list(dict.fromkeys(tag_ids.get(norm(planned.get("target_category"))) or []))
        if len(prop_matches) != 1:
            record["apply_status"] = "blocked_property_metadata"
            record["apply_reason"] = f"property ID matches={len(prop_matches)}"
            records.append(record)
            continue
        if len(tag_matches) != 1:
            record["apply_status"] = "blocked_tag_metadata"
            record["apply_reason"] = f"tag ID matches={len(tag_matches)}"
            records.append(record)
            continue
        record["target_property_id"] = prop_matches[0]
        record["target_tag_id"] = tag_matches[0]

        if live.get("pending"):
            record["apply_status"] = "blocked_pending"
        elif live.get("isSplit"):
            record["apply_status"] = "blocked_split_parent"
        elif live.get("hidden") or live.get("isDeleted"):
            record["apply_status"] = "blocked_inactive"
        else:
            current_property = str(live.get("propertyId") or "")
            current_tag = str(live.get("tagId") or "")
            if live.get("parentId") and current_tag:
                record["target_tag_id"] = current_tag
                record["apply_reason"] = (
                    "split leaf; preserved existing category; "
                    "generic property remains evidence-reviewable"
                    if record["target_property_id"] == "37648"
                    else "split leaf; preserved existing category"
                )
            if (
                current_property == record["target_property_id"]
                and current_tag == record["target_tag_id"]
            ):
                record["apply_status"] = "already_applied"
            elif current_property and current_property != record["target_property_id"]:
                record["apply_status"] = "blocked_property_conflict"
                record["apply_reason"] = f"live propertyId={current_property}"
            elif (
                live.get("parentId")
                and not str(planned.get("property_reason") or "").startswith("semantic_")
                and not (
                    record["target_property_id"] == "37648"
                    and current_tag
                )
            ):
                record["apply_status"] = "blocked_split_transaction"
                record["apply_reason"] = (
                    "split leaf lacks explicit property evidence or an existing category"
                )
            elif current_property == record["target_property_id"] or not current_property:
                record["apply_status"] = "ready"
            else:
                record["apply_status"] = "blocked_live_state"
        records.append(record)
    return records


def unchanged(record: dict[str, Any], live: dict[str, Any]) -> bool:
    current_property = str(live.get("propertyId") or "")
    return (
        live_key(live) == plan_key(record)
        and not live.get("pending")
        and not live.get("isSplit")
        and not live.get("parentId")
        and not live.get("hidden")
        and not live.get("isDeleted")
        and current_property in {"", str(record["target_property_id"])}
    )


def execute(records: list[dict[str, Any]], batch_size: int) -> tuple[int, int]:
    applied = 0
    failed = 0
    ready = [row for row in records if row["apply_status"] == "ready"]
    if not ready:
        return applied, failed
    for start in range(0, len(ready), batch_size):
        batch = ready[start : start + batch_size]
        try:
            updated = update_batch(batch)
        except Exception as exc:  # noqa: BLE001
            for row in batch:
                row["apply_status"] = "failed_update_request"
                row["apply_reason"] = str(exc)[-500:]
                failed += 1
            continue
        updated_ids = {str(row.get("id") or "") for row in updated}
        for row in batch:
            if row["baselane_id"] in updated_ids:
                row["apply_status"] = "updated_pending_full_readback"
            else:
                row["apply_status"] = "failed_update_not_returned"
                failed += 1

    post_rows, _ = fetch_all_transactions()
    post_by_id = {str(row.get("id") or ""): row for row in post_rows}
    for row in ready:
        if row["apply_status"] != "updated_pending_full_readback":
            continue
        live = post_by_id.get(row["baselane_id"]) or {}
        if (
            str(live.get("propertyId") or "") == row["target_property_id"]
            and str(live.get("tagId") or "") == row["target_tag_id"]
        ):
            row["apply_status"] = "applied_verified"
            applied += 1
        else:
            row["apply_status"] = "failed_post_write_verification"
            failed += 1
    return applied, failed


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
    digest = plan_digest(plan)
    if args.apply and os.environ.get(APPLY_ENV) != "1":
        raise SystemExit(f"--apply requires {APPLY_ENV}=1")
    if args.apply and args.require_plan_digest != digest:
        raise SystemExit("--apply requires the exact current --require-plan-digest")

    with exclusive_lock() as acquired:
        if not acquired:
            report = {
                "status": "locked",
                "generated_at": iso_z(),
                "mode": "apply" if args.apply else "dry_run",
                "plan_digest": digest,
                "lock": str(LOCK_PATH),
            }
            write_report(args.report, report)
            print(json.dumps(report, indent=2))
            return 75

        property_ids, tag_ids, _ = fetch_metadata()
        live_rows, live_total = fetch_all_transactions()
        records = classify(plan, live_rows, property_ids, tag_ids)
        ready = sum(row["apply_status"] == "ready" for row in records)
        already = sum(row["apply_status"] == "already_applied" for row in records)
        applied = failed = 0
        if args.apply:
            applied, failed = execute(records, args.batch_size)
        blocked = sum(row["apply_status"].startswith("blocked") for row in records)
        report = {
            "status": "ok" if blocked == 0 and failed == 0 else "review",
            "generated_at": iso_z(),
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
                {key: report[key] for key in report if key not in {"records"}},
                indent=2,
            )
        )
        return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
