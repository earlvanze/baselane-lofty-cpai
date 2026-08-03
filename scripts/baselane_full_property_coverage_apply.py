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
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator


ROOT = Path("/home/digit/.openclaw/workspace")
REPO_ROOT = Path(__file__).absolute().parents[1]
DROPBOX_REPORTS = Path(
    "/mnt/c/Users/digit/Dropbox/Projects/cyber-gateway/config/openclaw/workspace/reports"
)
DEFAULT_PLAN = DROPBOX_REPORTS / "baselane_full_property_coverage_plan.json"
DEFAULT_REPORT = DROPBOX_REPORTS / "baselane_full_property_coverage_apply.json"
BRIDGE = Path(
    os.environ.get(
        "BASELANE_GQL_BRIDGE",
        str(REPO_ROOT / "scripts" / "baselane_graphql_via_cdp.js"),
    )
)
LOCK_PATH = Path(
    os.environ.get("BASELANE_SOURCE_PIPELINE_LOCK", "/tmp/baselane-source-pipeline.lock")
)
APPLY_ENV = "BASELANE_FULL_PROPERTY_COVERAGE_APPLY"

sys.path.insert(0, str(ROOT / "skills" / "baselane-mcp" / "src"))
from baselane_mcp.transfers import (  # noqa: E402
    TransferStateError,
    run_graphql_batch_via_cdp,
    run_graphql_via_cdp,
)


TRANSACTIONS_QUERY = """
query Transactions($input: SortsAndFilters) {
  transactions(input: $input) {
    total
    data {
      id amount date merchantName description name pending propertyId tagId
      unitId note bankAccountId isManual tagIdSource propertyTagIdSource
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


def graphql_batch(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return run_graphql_batch_via_cdp(
        operations,
        bridge_path=BRIDGE,
        workspace_root=ROOT,
        timeout=180,
    )


def graphql_read(payload: dict[str, Any], attempts: int = 3) -> dict[str, Any]:
    """Retry transient unreadable CDP responses for read-only operations."""
    for attempt in range(1, attempts + 1):
        try:
            return graphql(payload)
        except TransferStateError:
            if attempt == attempts:
                raise
            time.sleep(attempt)
    raise AssertionError("unreachable")


def graphql_batch_read(
    operations: list[dict[str, Any]], attempts: int = 3
) -> list[dict[str, Any]]:
    """Retry transient unreadable CDP responses for read-only batches."""
    for attempt in range(1, attempts + 1):
        try:
            return graphql_batch(operations)
        except TransferStateError:
            if attempt == attempts:
                raise
            time.sleep(attempt)
    raise AssertionError("unreachable")


def fetch_metadata() -> tuple[dict[str, list[str]], dict[str, list[str]], dict[str, str]]:
    properties_payload, tags_payload = graphql_batch(
        [
            {
                "operationName": "PropertyList",
                "variables": {},
                "query": "query PropertyList { property { id name address } }",
            },
            {
                "operationName": "TagList",
                "variables": {},
                "query": (
                    "query TagList { tag { type subType { id name "
                    "subType { id name subType { id name } } } } }"
                ),
            },
        ]
    )
    property_ids: dict[str, list[str]] = defaultdict(list)
    property_names: dict[str, str] = {}
    for row in (properties_payload.get("data") or {}).get("property") or []:
        row_id = str(row.get("id") or "")
        name = str(row.get("name") or row.get("address") or "").strip()
        if row_id and name:
            property_ids[norm(name)].append(row_id)
            property_names[row_id] = name

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


def transaction_operation(page: int, page_limit: int) -> dict[str, Any]:
    return {
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


def transaction_result(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    result = (payload.get("data") or {}).get("transactions") or {}
    return result.get("data") or [], int(result.get("total") or 0)


def fetch_all_transactions(
    page_limit: int = 500, operation_batch_size: int = 25
) -> tuple[list[dict[str, Any]], int]:
    if page_limit < 1 or operation_batch_size < 1:
        raise ValueError("page_limit and operation_batch_size must be positive")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    first_batch, total = transaction_result(
        graphql_read(transaction_operation(1, page_limit))
    )
    page_count = (total + page_limit - 1) // page_limit
    payloads = [(first_batch, total)]
    remaining = [
        transaction_operation(page, page_limit) for page in range(2, page_count + 1)
    ]
    if operation_batch_size == 1:
        for operation in remaining:
            payloads.append(transaction_result(graphql_read(operation)))
    else:
        for start in range(0, len(remaining), operation_batch_size):
            for payload in graphql_batch_read(
                remaining[start : start + operation_batch_size]
            ):
                payloads.append(transaction_result(payload))

    for batch, reported_total in payloads:
        if reported_total != total:
            raise RuntimeError(
                f"live pagination total changed: first={total} page={reported_total}"
            )
        for row in batch:
            row_id = str(row.get("id") or "")
            if not row_id or row_id in seen:
                raise RuntimeError(f"duplicate or missing transaction ID during pagination: {row_id!r}")
            seen.add(row_id)
            rows.append(row)
    if total != len(rows):
        raise RuntimeError(f"live pagination incomplete: fetched={len(rows)} total={total}")
    return rows, total


def verify_by_ids(ids: list[str], operation_size: int = 50) -> dict[str, dict[str, Any]]:
    unique_ids = list(dict.fromkeys(str(row_id) for row_id in ids if row_id))
    operations = []
    for start in range(0, len(unique_ids), operation_size):
        fields = [
            f't{index}: transaction(id: {json.dumps(row_id)}) {{ {VERIFY_FIELDS} }}'
            for index, row_id in enumerate(unique_ids[start : start + operation_size])
        ]
        operations.append(
            {
                "operationName": "VerifyCoverageTransactions",
                "variables": {},
                "query": (
                    "query VerifyCoverageTransactions {\n"
                    + "\n".join(fields)
                    + "\n}"
                ),
            }
        )
    payloads = graphql_batch(operations)
    return {
        str(row.get("id")): row
        for payload in payloads
        for row in (payload.get("data") or {}).values()
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
    return (
        live_key(live) == plan_key(record)
        and not live.get("pending")
        and not live.get("isSplit")
        and not live.get("hidden")
        and not live.get("isDeleted")
        and str(live.get("parentId") or "") == str(record.get("parent_id") or "")
        and str(live.get("propertyId") or "")
        == str(record.get("current_property_id") or "")
        and str(live.get("tagId") or "") == str(record.get("current_tag_id") or "")
    )


def execute(records: list[dict[str, Any]], batch_size: int) -> tuple[int, int]:
    applied = 0
    failed = 0
    ready = [row for row in records if row["apply_status"] == "ready"]
    if not ready:
        return applied, failed
    pre_by_id = verify_by_ids([row["baselane_id"] for row in ready])
    for row in ready:
        live = pre_by_id.get(row["baselane_id"]) or {}
        if not unchanged(row, live):
            row["apply_status"] = "blocked_pre_write_state_changed"
            row["apply_reason"] = "exact-ID live state no longer matches classified snapshot"
    ready = [row for row in ready if row["apply_status"] == "ready"]
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
                row["apply_status"] = "updated_pending_exact_readback"
            else:
                row["apply_status"] = "failed_update_not_returned"
                failed += 1

    post_by_id = verify_by_ids(
        [
            row["baselane_id"]
            for row in ready
            if row["apply_status"] == "updated_pending_exact_readback"
        ]
    )
    for row in ready:
        if row["apply_status"] != "updated_pending_exact_readback":
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
