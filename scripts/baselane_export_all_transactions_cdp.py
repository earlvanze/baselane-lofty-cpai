#!/usr/bin/env python3
"""Export every active Baselane transaction from the authenticated CDP session."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import baselane_full_property_coverage_apply as coverage


FIELDS = [
    "Account",
    "Date",
    "Merchant",
    "Description",
    "Amount",
    "Type",
    "Category",
    "Sub-category",
    "Property",
    "Unit",
    "Notes",
]


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def export_metadata() -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    properties_payload, tags_payload = coverage.graphql_batch_read(
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
    properties = {
        str(row.get("id")): str(row.get("name") or row.get("address") or "").strip()
        for row in (properties_payload.get("data") or {}).get("property") or []
        if row.get("id")
    }
    tags: dict[str, tuple[str, str]] = {}

    def walk(items: list[dict[str, Any]], transaction_type: str) -> None:
        for item in items:
            item_id = str(item.get("id") or "")
            name = str(item.get("name") or "").strip()
            if item_id:
                tags[item_id] = (transaction_type, name)
            children = item.get("subType")
            if isinstance(children, list):
                walk(children, transaction_type)

    for group in (tags_payload.get("data") or {}).get("tag") or []:
        walk(group.get("subType") or [], str(group.get("type") or ""))
    return properties, tags


def display_date(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").strftime("%B %d, %Y")
    except ValueError:
        return raw


def note_text(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or "")
    return str(value or "")


def csv_rows(
    transactions: list[dict[str, Any]],
    properties: dict[str, str],
    tags: dict[str, tuple[str, str]],
) -> list[dict[str, object]]:
    result = []
    for transaction in transactions:
        property_id = str(transaction.get("propertyId") or "")
        tag_id = str(transaction.get("tagId") or "")
        transaction_type, category = tags.get(tag_id, ("", ""))
        merchant = str(transaction.get("merchantName") or "")
        result.append(
            {
                "Account": "",
                "Date": display_date(transaction.get("date")),
                "Merchant": merchant,
                "Description": str(
                    transaction.get("description")
                    or transaction.get("name")
                    or merchant
                ),
                "Amount": transaction.get("amount"),
                "Type": transaction_type,
                "Category": category,
                "Sub-category": "",
                "Property": properties.get(property_id, ""),
                "Unit": str(transaction.get("unitId") or ""),
                "Notes": note_text(transaction.get("note")),
            }
        )
    return result


def atomic_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        newline="",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=Path(
            os.environ.get(
                "BASELANE_REPORT_DIR",
                "/home/digit/.openclaw/workspace/reports",
            )
        ),
    )
    # CDP Runtime.evaluate responses are truncated at roughly 64 KiB. Keep a
    # transaction page below that transport limit; completeness is still
    # enforced against Baselane's reported total after pagination.
    parser.add_argument("--page-limit", type=int, default=500)
    # The CDP helper returns the whole operation batch as one JSON document on
    # stdout, which is subject to the same transport cap as a single page.
    parser.add_argument("--operation-batch-size", type=int, default=1)
    args = parser.parse_args()

    with coverage.exclusive_lock() as acquired:
        if not acquired:
            print("Baselane source pipeline lock is held", flush=True)
            return 75
        generated_at = iso_z()
        transactions, total = coverage.fetch_all_transactions(
            page_limit=args.page_limit,
            operation_batch_size=args.operation_batch_size,
        )
        properties, tags = export_metadata()

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    output = (
        args.report_dir.resolve()
        / f"baselane_export_all_transactions.{timestamp}.csv"
    )
    rows = csv_rows(transactions, properties, tags)
    atomic_csv(output, rows)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    report = output.with_suffix(".json")
    atomic_json(
        report,
        {
            "status": "ok",
            "generated_at": generated_at,
            "source": "live_baselane_cdp",
            "sort": {"field": "id", "direction": "DESC"},
            "active_filter": {"isHidden": False, "isDeleted": False},
            "reported_total": total,
            "row_count": len(rows),
            "unique_transaction_id_count": len(
                {str(row.get("id") or "") for row in transactions}
            ),
            "output": str(output),
            "sha256": digest,
        },
    )
    print(
        json.dumps(
            {
                "status": "ok",
                "rows": len(rows),
                "output": str(output),
                "report": str(report),
                "sha256": digest,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
