#!/usr/bin/env python3
"""Idempotently apply scoped 2026 DAO capital and transfer-clearing entries."""

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


DEFAULT_TARGETS = ROOT / "reports" / "scoped_dao_2026_capital_model.json"
DEFAULT_REPORT = ROOT / "reports" / "scoped_dao_2026_capital_live_apply.json"
RESET_PREFIX = "ECO-DAO-2026-CAPITAL"
RETAINED_MARKER = "AOPS-PNL-ACCRUAL|retained_capital|"
EXPECTED_TAG_ID = "25"
LEGACY_RETAINED = {
    ("2025-07-28", Decimal("-750.00")),
    ("2025-08-28", Decimal("-2150.00")),
    ("2025-10-28", Decimal("-200.00")),
    ("2025-11-28", Decimal("-1000.00")),
    ("2026-04-28", Decimal("-200.00")),
    ("2026-05-28", Decimal("-100.00")),
    ("2026-06-28", Decimal("-1000.00")),
}
LEGACY_PROPERTY_ID = "31525"


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
    rows = run_graphql({
        "operationName": "PropertyList",
        "variables": {},
        "query": "query PropertyList { property { id name } }",
    })["data"]["property"]
    return {str(row["id"]): str(row["name"]) for row in rows}


def query_owner_tag() -> list[dict[str, str]]:
    roots = run_graphql({
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

    for root in roots:
        visit(root.get("subType") or [])
    return [row for row in flattened if row["name"] == "Owner Contributions/Distributions"]


def monthly_totals(rows: list[dict[str, Any]], include_resets: bool) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for row in rows:
        date = str(row.get("date") or "")
        if not ("2026-01-01" <= date <= "2026-06-30"):
            continue
        if not include_resets and note_text(row.get("note")).startswith(RESET_PREFIX):
            continue
        totals[date[:7]] += Decimal(str(row.get("amount") or 0))
    return totals


def index_existing(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        note = note_text(row.get("note"))
        if note.startswith(RESET_PREFIX):
            result[note.split(" | ", 1)[0]].append(row)
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
    query = "mutation CreateDaoCapitalEntries {\n" + "\n".join(fields) + "\n}"
    data = run_graphql({"operationName": "CreateDaoCapitalEntries", "variables": {}, "query": query})["data"]
    return [data[f"r{index}"] for index in range(len(targets))]


def update_transaction(live: dict[str, Any], *, amount: Decimal, note: str, tag_id: str, property_id: str) -> dict[str, Any]:
    rows = run_graphql({
        "operationName": "UpdateTransaction",
        "variables": {
            "input": [{
                "id": str(live["id"]),
                "amount": float(amount),
                "note": note,
                "tagId": tag_id,
                "propertyId": property_id,
                "unitId": None,
            }]
        },
        "query": """
        mutation UpdateTransaction($input: [UpdateTransaction!]) {
          updateTransactions(input: $input) { id amount date propertyId tagId note isManual }
        }
        """,
    })["data"]["updateTransactions"]
    return rows[0]


def legacy_retained_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if RETAINED_MARKER in note_text(row.get("note"))]


def corrected_legacy_note(row: dict[str, Any]) -> str:
    note = note_text(row.get("note"))
    suffix = "Legacy retained-capital pseudo-entry; reclassified from Utilities to Owner Contributions/Distributions and superseded by ECO-DAO-2026-CAPITAL rollforward."
    return note if suffix in note else f"{note} | {suffix}"


def obsolete_reset_note(row: dict[str, Any]) -> str:
    marker = note_text(row.get("note")).split(" | ", 1)[0]
    return (
        f"{marker} | Amount: 0.00. Obsolete month-end component zeroed after "
        "source-account category correction; no bank transfer represented."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--require-target-digest", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    source = json.loads(args.targets.read_text(encoding="utf-8"))
    targets = source["reset_targets"]
    digest = digest_targets(targets)
    if digest != source["reset_targets_digest_sha256"] or digest != args.require_target_digest:
        raise RuntimeError("target digest mismatch")
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

    planned: list[dict[str, Any]] = []
    cached_rows: dict[str, list[dict[str, Any]]] = {}
    for property_id, property_targets in by_property.items():
        rows = query_transactions(property_id)
        cached_rows[property_id] = rows
        source_totals = monthly_totals(rows, include_resets=False)
        existing = index_existing(rows)
        expected_month_totals = {target["month"]: Decimal(target["source_month_total"]) for target in property_targets}
        for month, expected_total in expected_month_totals.items():
            live_total = source_totals.get(month, Decimal("0")).quantize(Decimal("0.01"))
            if live_total != expected_total:
                raise RuntimeError(f"live source drift for {properties[property_id]} {month}: live={live_total} target={expected_total}")
        for target in property_targets:
            marker = target["note"].split(" | ", 1)[0]
            found = existing.get(marker, [])
            if len(found) > 1:
                raise RuntimeError(f"duplicate live capital marker: {marker}")
            action = "create" if not found else ("skip" if matches(target, found[0]) else "update")
            planned.append({"action": action, "target": target, "live": found[0] if found else None})

    legacy = legacy_retained_rows(cached_rows[LEGACY_PROPERTY_ID])
    observed_legacy = {(str(row["date"]), Decimal(str(row["amount"])).quantize(Decimal("0.01"))) for row in legacy}
    if observed_legacy != LEGACY_RETAINED:
        raise RuntimeError(f"unexpected legacy retained rows: {sorted(observed_legacy)}")
    legacy_planned = [
        {
            "action": "skip" if str(row.get("tagId")) == EXPECTED_TAG_ID and note_text(row.get("note")) == corrected_legacy_note(row) else "update",
            "live": row,
            "corrected_note": corrected_legacy_note(row),
        }
        for row in legacy
    ]
    target_markers = {target["note"].split(" | ", 1)[0] for target in targets}
    obsolete_planned: list[dict[str, Any]] = []
    for property_rows in cached_rows.values():
        for row in property_rows:
            note = note_text(row.get("note"))
            marker = note.split(" | ", 1)[0]
            date = str(row.get("date") or "")
            if not note.startswith(RESET_PREFIX) or not ("2026-01-01" <= date <= "2026-06-30"):
                continue
            if marker in target_markers:
                continue
            target_note = obsolete_reset_note(row)
            is_zero = Decimal(str(row.get("amount") or 0)).quantize(Decimal("0.01")) == Decimal("0.00")
            obsolete_planned.append({
                "action": "skip" if is_zero and note == target_note else "update",
                "live": row,
                "corrected_note": target_note,
            })

    created: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []
    legacy_updated: list[dict[str, Any]] = []
    if args.apply:
        for item in obsolete_planned:
            if item["action"] == "update":
                row = item["live"]
                updated.append(update_transaction(
                    row, amount=Decimal("0.00"), note=item["corrected_note"],
                    tag_id=EXPECTED_TAG_ID, property_id=str(row["propertyId"]),
                ))
        for item in legacy_planned:
            if item["action"] == "update":
                row = item["live"]
                legacy_updated.append(update_transaction(
                    row,
                    amount=Decimal(str(row["amount"])),
                    note=item["corrected_note"],
                    tag_id=EXPECTED_TAG_ID,
                    property_id=LEGACY_PROPERTY_ID,
                ))
        create_targets = [item["target"] for item in planned if item["action"] == "create"]
        for offset in range(0, len(create_targets), 5):
            created.extend(create_batch(create_targets[offset:offset + 5]))
        for item in planned:
            if item["action"] == "update":
                target = item["target"]
                updated.append(update_transaction(
                    item["live"], amount=Decimal(target["amount"]), note=target["note"],
                    tag_id=target["tag_id"], property_id=target["property_id"],
                ))

    verification: list[dict[str, Any]] = []
    legacy_verification: list[dict[str, Any]] = []
    if args.apply:
        for property_id, property_targets in by_property.items():
            rows = query_transactions(property_id)
            totals = monthly_totals(rows, include_resets=True)
            existing = index_existing(rows)
            for month in sorted({target["month"] for target in property_targets}):
                month_targets = [target for target in property_targets if target["month"] == month]
                marker_results = []
                for target in month_targets:
                    marker = target["note"].split(" | ", 1)[0]
                    found = existing.get(marker, [])
                    marker_results.append(len(found) == 1 and matches(target, found[0]))
                balance = totals.get(month, Decimal("0")).quantize(Decimal("0.01"))
                verification.append({
                    "property": properties[property_id], "month": month,
                    "post_capital_balance": f"{balance:.2f}",
                    "marker_count": len(month_targets), "ok": all(marker_results) and balance == Decimal("0.00"),
                })
        live_legacy = legacy_retained_rows(query_transactions(LEGACY_PROPERTY_ID))
        for row in live_legacy:
            legacy_verification.append({
                "transaction_id": str(row["id"]), "date": str(row["date"]),
                "tag_id": str(row.get("tagId")),
                "ok": str(row.get("tagId")) == EXPECTED_TAG_ID and note_text(row.get("note")) == corrected_legacy_note(row),
            })
        failures = [row for row in verification + legacy_verification if not row["ok"]]
        if failures:
            raise RuntimeError(f"post-apply verification failed: {json.dumps(failures[:10], indent=2)}")

    report = {
        "generated_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
        "mode": "apply" if args.apply else "dry_run",
        "targets": str(args.targets),
        "targets_digest_sha256": digest,
        "planned_counts": {action: sum(1 for row in planned if row["action"] == action) for action in ("create", "update", "skip")},
        "legacy_retained_planned_counts": {action: sum(1 for row in legacy_planned if row["action"] == action) for action in ("update", "skip")},
        "obsolete_reset_planned_counts": {action: sum(1 for row in obsolete_planned if row["action"] == action) for action in ("update", "skip")},
        "created": created, "updated": updated, "legacy_retained_updated": legacy_updated,
        "verification": verification, "legacy_retained_verification": legacy_verification,
        "verification_failures": sum(1 for row in verification + legacy_verification if not row["ok"]),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=True, default=str) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("mode", "targets_digest_sha256", "planned_counts", "legacy_retained_planned_counts", "verification_failures")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
