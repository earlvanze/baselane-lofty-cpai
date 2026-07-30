#!/usr/bin/env python3
"""Idempotently apply the scoped 2024-2025 DAO cash reset entries."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any


ROOT = Path(os.environ.get("OPENCLAW_WORKSPACE", "/home/digit/.openclaw/workspace"))
sys.path.insert(0, str(ROOT / "scripts"))

from baselane_apply_alcott_accruals_live import run_graphql  # noqa: E402


DEFAULT_TARGETS = ROOT / "reports" / "scoped_dao_cash_reconciliation_dry_run.json"
DEFAULT_REPORT = ROOT / "reports" / "scoped_dao_cash_reconciliation_live_apply.json"
RESET_PREFIX = "ECO-DAO-MONTH-END-RESET"
EXPECTED_TAG_ID = "25"


def note_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or "")
    return str(value or "")


def digest_targets(targets: list[dict[str, Any]]) -> str:
    payload = json.dumps(targets, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def query_transactions(property_id: str) -> list[dict[str, Any]]:
    query = """
    query Transactions($input: SortsAndFilters) {
      transactions(input: $input) {
        total
        data { id amount date merchantName propertyId tagId note isManual hidden isDeleted }
      }
    }
    """
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        result = run_graphql({
            "operationName": "Transactions",
            "variables": {
                "input": {
                    "sort": {"direction": "DESC", "field": "date"},
                    "filter": {"propertyId": property_id, "isHidden": False, "isDeleted": False},
                    "page": page,
                    "pageLimit": 1000,
                }
            },
            "query": query,
        })["data"]["transactions"]
        batch = result.get("data") or []
        rows.extend(batch)
        if not batch or len(rows) >= int(result.get("total") or 0):
            return rows
        page += 1


def query_properties() -> dict[str, str]:
    result = run_graphql({
        "operationName": "PropertyList",
        "variables": {},
        "query": "query PropertyList { property { id name } }",
    })["data"]["property"]
    return {str(row["id"]): str(row["name"]) for row in result}


def query_owner_tag() -> list[dict[str, str]]:
    result = run_graphql({
        "operationName": "TagList",
        "variables": {},
        "query": "query TagList { tag { type subType { id name subType { id name subType { id name } } } } }",
    })["data"]["tag"]
    flattened: list[dict[str, str]] = []

    def visit(rows: list[dict[str, Any]]) -> None:
        for row in rows or []:
            if row.get("id") is not None and row.get("name") is not None:
                flattened.append({"id": str(row["id"]), "name": str(row["name"])})
            visit(row.get("subType") or [])

    for root in result:
        visit(root.get("subType") or [])
    return [row for row in flattened if row["name"] == "Owner Contributions/Distributions"]


def monthly_totals(rows: list[dict[str, Any]], include_resets: bool) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for row in rows:
        date = str(row.get("date") or "")
        if not ("2024-01-01" <= date <= "2025-12-31"):
            continue
        if not include_resets and RESET_PREFIX in note_text(row.get("note")):
            continue
        totals[date[:7]] += Decimal(str(row.get("amount") or 0))
    return totals


def index_existing(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        note = note_text(row.get("note"))
        if RESET_PREFIX not in note:
            continue
        marker = note.split(" | ", 1)[0]
        result[marker].append(row)
    return result


def matches(target: dict[str, Any], live: dict[str, Any]) -> bool:
    return (
        Decimal(str(live.get("amount") or 0)).quantize(Decimal("0.01")) == Decimal(target["amount"])
        and str(live.get("propertyId") or "") == str(target["property_id"])
        and str(live.get("tagId") or "") == str(target["tag_id"])
        and str(live.get("date") or "") == str(target["date"])
        and note_text(live.get("note")) == str(target["note"])
    )


def gql_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def create_batch(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields: list[str] = []
    for index, target in enumerate(targets):
        fields.append(
            f"r{index}: createTransaction(input: {{"
            f"merchantName: {gql_string(target['merchant_name'])} "
            f"note: {gql_string(target['note'])} "
            f"tagId: {gql_string(str(target['tag_id']))} "
            f"propertyId: {gql_string(str(target['property_id']))} "
            "unitId: null entityId: null "
            f"date: {gql_string(target['date'])} "
            "bankAccountId: null "
            f"amount: {target['amount']} isReviewedByUser: true"
            "}) { id amount date propertyId tagId note isManual }"
        )
    query = "mutation CreateDaoCashResets {\n" + "\n".join(fields) + "\n}"
    result = run_graphql({"operationName": "CreateDaoCashResets", "variables": {}, "query": query})["data"]
    return [result[f"r{index}"] for index in range(len(targets))]


def update_transaction(target: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    result = run_graphql({
        "operationName": "UpdateTransaction",
        "variables": {
            "input": [{
                "id": str(live["id"]),
                "amount": float(target["amount"]),
                "note": target["note"],
                "tagId": target["tag_id"],
                "propertyId": target["property_id"],
                "unitId": None,
            }]
        },
        "query": """
        mutation UpdateTransaction($input: [UpdateTransaction!]) {
          updateTransactions(input: $input) { id amount date propertyId tagId note isManual }
        }
        """,
    })["data"]["updateTransactions"]
    return result[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--require-target-digest", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    source = json.loads(args.targets.read_text(encoding="utf-8"))
    targets = source["monthly_resets"]["targets"]
    digest = digest_targets(targets)
    expected_digest = str(source["monthly_resets"]["targets_digest_sha256"])
    if digest != expected_digest or digest != args.require_target_digest:
        raise RuntimeError(f"target digest mismatch: calculated={digest} report={expected_digest} required={args.require_target_digest}")
    if any(str(target["tag_id"]) != EXPECTED_TAG_ID for target in targets):
        raise RuntimeError("target contains an unexpected category tag")

    properties = query_properties()
    owner_tags = query_owner_tag()
    if owner_tags != [{"id": EXPECTED_TAG_ID, "name": "Owner Contributions/Distributions"}]:
        raise RuntimeError(f"live owner tag mismatch: {owner_tags}")

    by_property: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for target in targets:
        by_property[str(target["property_id"])].append(target)
    for property_id, property_targets in by_property.items():
        expected_name = str(property_targets[0]["property"])
        if properties.get(property_id) != expected_name:
            raise RuntimeError(f"property mismatch: {property_id} live={properties.get(property_id)} expected={expected_name}")

    live_by_property: dict[str, list[dict[str, Any]]] = {}
    existing_by_property: dict[str, dict[str, list[dict[str, Any]]]] = {}
    planned: list[dict[str, Any]] = []
    for property_id, property_targets in by_property.items():
        rows = query_transactions(property_id)
        live_by_property[property_id] = rows
        source_totals = monthly_totals(rows, include_resets=False)
        existing = index_existing(rows)
        existing_by_property[property_id] = existing
        for target in property_targets:
            live_source = source_totals.get(target["month"], Decimal("0")).quantize(Decimal("0.01"))
            if live_source != Decimal(target["source_month_total"]):
                raise RuntimeError(
                    f"live source drift for {target['property']} {target['month']}: "
                    f"live={live_source} target={target['source_month_total']}"
                )
            marker = target["note"].split(" | ", 1)[0]
            matches_for_marker = existing.get(marker, [])
            if len(matches_for_marker) > 1:
                raise RuntimeError(f"duplicate live reset marker: {marker}")
            if not matches_for_marker:
                action = "create"
                live = None
            elif matches(target, matches_for_marker[0]):
                action = "skip"
                live = matches_for_marker[0]
            else:
                action = "update"
                live = matches_for_marker[0]
            planned.append({"action": action, "target": target, "live": live})

    created: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []
    if args.apply:
        create_targets = [item["target"] for item in planned if item["action"] == "create"]
        for offset in range(0, len(create_targets), 5):
            created.extend(create_batch(create_targets[offset : offset + 5]))
        for item in planned:
            if item["action"] == "update":
                updated.append(update_transaction(item["target"], item["live"]))

    verification: list[dict[str, Any]] = []
    if args.apply:
        for property_id, property_targets in by_property.items():
            rows = query_transactions(property_id)
            totals = monthly_totals(rows, include_resets=True)
            existing = index_existing(rows)
            for target in property_targets:
                marker = target["note"].split(" | ", 1)[0]
                live = existing.get(marker, [])
                balance = totals.get(target["month"], Decimal("0")).quantize(Decimal("0.01"))
                ok = len(live) == 1 and matches(target, live[0]) and balance == Decimal("0.00")
                verification.append({
                    "property": target["property"],
                    "month": target["month"],
                    "marker": marker,
                    "live_count": len(live),
                    "post_reset_balance": f"{balance:.2f}",
                    "ok": ok,
                    "transaction_id": str(live[0]["id"]) if len(live) == 1 else None,
                })
        failures = [row for row in verification if not row["ok"]]
        if failures:
            raise RuntimeError(f"post-apply verification failed: {json.dumps(failures[:10], indent=2)}")

    report = {
        "generated_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
        "mode": "apply" if args.apply else "dry_run",
        "targets": str(args.targets),
        "targets_digest_sha256": digest,
        "planned_counts": {action: sum(1 for row in planned if row["action"] == action) for action in ("create", "update", "skip")},
        "created": created,
        "updated": updated,
        "verification": verification,
        "verification_failures": sum(1 for row in verification if not row["ok"]),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=True, default=str) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("mode", "targets_digest_sha256", "planned_counts", "verification_failures")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
