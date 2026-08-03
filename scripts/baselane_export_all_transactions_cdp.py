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


ROOT = Path(__file__).resolve().parents[1]
ARCHIVED_PROPERTY_ALIASES_PATH = (
    ROOT / "config" / "baselane_archived_property_id_aliases.json"
)

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

SOURCE_INDEX_FIELDS = [
    *FIELDS,
    "BaselaneId",
    "ISODate",
    "PropertyId",
    "TagId",
    "TagIdSource",
    "PropertyTagIdSource",
    "BankAccountId",
    "Pending",
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


def load_archived_property_aliases(
    path: Path = ARCHIVED_PROPERTY_ALIASES_PATH,
) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    aliases: dict[str, str] = {}
    for raw_id, item in (payload.get("aliases") or {}).items():
        property_id = str(raw_id).strip()
        property_name = str((item or {}).get("property") or "").strip()
        if not property_id or not property_name:
            raise RuntimeError(f"invalid archived property alias in {path}: {raw_id!r}")
        aliases[property_id] = property_name
    return aliases


def merge_property_metadata(
    active: dict[str, str], archived: dict[str, str]
) -> dict[str, str]:
    conflicts = {
        property_id: (active[property_id], archived_name)
        for property_id, archived_name in archived.items()
        if property_id in active and active[property_id] != archived_name
    }
    if conflicts:
        raise RuntimeError(f"archived property alias conflicts with live metadata: {conflicts}")
    return {**archived, **active}


def export_bank_accounts() -> dict[str, str]:
    payload = coverage.graphql_read(
        {
            "operationName": "BankAccounts",
            "variables": {},
            "query": (
                "query BankAccounts { bankAccounts { id accountName nickName "
                "accountNumber institutionName } }"
            ),
        }
    )
    accounts: dict[str, str] = {}
    for row in (payload.get("data") or {}).get("bankAccounts") or []:
        account_id = str(row.get("id") or "").strip()
        if not account_id:
            continue
        account_number = str(row.get("accountNumber") or "").strip()
        suffix = account_number[-4:] if account_number else ""
        accounts[account_id] = "-".join(
            part
            for part in (
                str(row.get("accountName") or "").strip(),
                str(row.get("nickName") or "").strip(),
                suffix,
            )
            if part
        )
    return accounts


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


def source_index_rows(
    transactions: list[dict[str, Any]],
    properties: dict[str, str],
    tags: dict[str, tuple[str, str]],
    accounts: dict[str, str],
) -> list[dict[str, object]]:
    rows = csv_rows(transactions, properties, tags)
    for row, transaction in zip(rows, transactions, strict=True):
        bank_account_id = str(transaction.get("bankAccountId") or "")
        property_id = str(transaction.get("propertyId") or "")
        tag_id = str(transaction.get("tagId") or "")
        pending = transaction.get("pending")
        row.update(
            {
                "Account": accounts.get(bank_account_id, ""),
                "BaselaneId": str(transaction.get("id") or ""),
                "ISODate": str(transaction.get("date") or "")[:10],
                "PropertyId": property_id,
                "TagId": tag_id,
                "TagIdSource": str(transaction.get("tagIdSource") or ""),
                "PropertyTagIdSource": str(
                    transaction.get("propertyTagIdSource") or ""
                ),
                "BankAccountId": bank_account_id,
                "Pending": (
                    "true" if pending is True else "false" if pending is False else ""
                ),
            }
        )
    return rows


def atomic_csv(
    path: Path,
    rows: list[dict[str, object]],
    fieldnames: list[str] | None = None,
) -> None:
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
        writer = csv.DictWriter(handle, fieldnames=fieldnames or FIELDS)
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
        active_properties, tags = export_metadata()
        accounts = export_bank_accounts()

    archived_property_aliases = load_archived_property_aliases()
    properties = merge_property_metadata(active_properties, archived_property_aliases)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    output = (
        args.report_dir.resolve()
        / f"baselane_export_all_transactions.{timestamp}.csv"
    )
    rows = csv_rows(transactions, properties, tags)
    atomic_csv(output, rows)
    index_rows = source_index_rows(transactions, properties, tags, accounts)
    unique_ids = {
        str(row.get("BaselaneId") or "") for row in index_rows if row.get("BaselaneId")
    }
    missing_account_ids = sorted(
        {
            str(row.get("BankAccountId") or "")
            for row in index_rows
            if row.get("BankAccountId") and not row.get("Account")
        }
    )
    missing_property_ids = sorted(
        {
            str(row.get("PropertyId") or "")
            for row in index_rows
            if row.get("PropertyId") and not row.get("Property")
        }
    )
    missing_tag_ids = sorted(
        {
            str(row.get("TagId") or "")
            for row in index_rows
            if row.get("TagId") and not row.get("Category")
        }
    )
    archived_property_alias_ids_used = sorted(
        {
            str(row.get("PropertyId") or "")
            for row in index_rows
            if str(row.get("PropertyId") or "") in archived_property_aliases
        }
    )
    violations = []
    if len(index_rows) != total:
        violations.append(f"row_count_mismatch:{len(index_rows)}!={total}")
    if len(unique_ids) != total:
        violations.append(f"unique_id_count_mismatch:{len(unique_ids)}!={total}")
    if missing_account_ids:
        violations.append(f"missing_account_metadata:{len(missing_account_ids)}")
    if missing_property_ids:
        violations.append(f"missing_property_metadata:{len(missing_property_ids)}")
    if missing_tag_ids:
        violations.append(f"missing_tag_metadata:{len(missing_tag_ids)}")
    if violations:
        raise RuntimeError("source transaction index guard failed: " + "; ".join(violations))

    source_index = args.report_dir.resolve() / "baselane_source_transaction_index.csv"
    source_index_snapshot = (
        args.report_dir.resolve()
        / f"baselane_source_transaction_index.{timestamp}.csv"
    )
    atomic_csv(source_index_snapshot, index_rows, SOURCE_INDEX_FIELDS)
    atomic_csv(source_index, index_rows, SOURCE_INDEX_FIELDS)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    source_index_digest = hashlib.sha256(source_index.read_bytes()).hexdigest()
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
            "source_transaction_index": str(source_index),
            "source_transaction_index_snapshot": str(source_index_snapshot),
            "source_transaction_index_sha256": source_index_digest,
            "source_transaction_index_current_write_status": "written_current",
            "bank_account_metadata_count": len(accounts),
            "active_property_metadata_count": len(active_properties),
            "archived_property_alias_count": len(archived_property_aliases),
            "archived_property_alias_ids_used": archived_property_alias_ids_used,
            "archived_property_alias_policy": str(ARCHIVED_PROPERTY_ALIASES_PATH),
            "archived_property_alias_policy_sha256": hashlib.sha256(
                ARCHIVED_PROPERTY_ALIASES_PATH.read_bytes()
            ).hexdigest(),
            "missing_account_metadata_ids": missing_account_ids,
            "missing_property_metadata_ids": missing_property_ids,
            "missing_tag_metadata_ids": missing_tag_ids,
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
                "source_transaction_index": str(source_index),
                "source_transaction_index_sha256": source_index_digest,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
