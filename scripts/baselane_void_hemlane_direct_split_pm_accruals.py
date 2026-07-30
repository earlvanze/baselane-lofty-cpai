#!/usr/bin/env python3
"""Idempotently void legacy Baselane PM rows already paid by Hemlane split.

The decision is based on transaction-level Hemlane rent evidence, never the
property's state. Historical accountless PM accrual and settlement rows
double-count the fee only when the month's rent was actually remitted net by
Hemlane and no direct gross rent remains as a manual-accrual basis. This tool
freezes the exact live row identities, produces a digest-bound dry run, and only
then updates those manual rows to zero.

No cash moves, and no category/tag is changed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from baselane_apply_alcott_accruals_live import run_graphql
from baselane_monthly_accruals_idempotent import (
    canonical_accrual_property_name,
    compute_pm_fees,
    hemlane_net_rent_amount,
    is_pm_accrual_kind,
    parse_marker,
    parse_pm_fee_marker,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "reports" / "baselane_source_transaction_index.csv"
DEFAULT_REPORT = ROOT / "reports" / "baselane_hemlane_direct_split_pm_void.json"
APPLY_ENV = "BASELANE_HEMLANE_DIRECT_PM_VOID_APPLY"
APPLY_DIGEST_ENV = "BASELANE_HEMLANE_DIRECT_PM_VOID_DIGEST"


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def note_text(value: Any) -> str:
    return str(value.get("text") or "") if isinstance(value, dict) else str(value or "")


def cents(value: Any) -> float:
    return round(float(str(value or "0").replace(",", "")), 2)


def zero_marker(marker: dict[str, str]) -> str:
    if marker["prefix"] == "AOPS-PM-FEE" and marker["kind"] == "pm":
        return f"{marker['prefix']}|{marker['property']}|{marker['month']}|0.00"
    return (
        f"{marker['prefix']}|{marker['kind']}|{marker['property']}|"
        f"{marker['month']}|0.00"
    )


def void_note(target: dict[str, Any]) -> str:
    return (
        f"{zero_marker(target['marker'])} | Voided legacy manual PM row of "
        f"${abs(float(target['amount'])):.2f}: Hemlane withheld/remitted the PM fee before "
        "depositing net rent into Baselane, so a Baselane accrual or settlement would "
        "double-count it. Accounting correction only; no cash movement. "
        f"Original Baselane transaction {target['id']}."
    )


def void_merchant(target: dict[str, Any]) -> str:
    return (
        f"Hemlane Direct-Split PM Void | {target['marker']['property']} | "
        f"{target['marker']['month']}"
    )


def source_targets(source_path: Path) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    with source_path.open(newline="", encoding="utf-8-sig") as handle:
        source_rows = list(csv.DictReader(handle))
    months = {
        marker["month"]
        for row in source_rows
        if (marker := (parse_marker(str(row.get("Notes") or "")) or parse_pm_fee_marker(str(row.get("Notes") or ""))))
    }
    expected_by_month = {
        month: compute_pm_fees(source_rows, month)
        for month in months
    }
    for csv_row, row in enumerate(source_rows, start=2):
        note = str(row.get("Notes") or "")
        marker = parse_marker(note) or parse_pm_fee_marker(note)
        if not marker:
            continue
        property_name = canonical_accrual_property_name(marker["property"])
        kind = marker["kind"]
        if not (is_pm_accrual_kind(kind) or kind == "pm_settlement"):
            continue
        month = marker["month"]
        if property_name in expected_by_month.get(month, {}):
            continue
        if hemlane_net_rent_amount(source_rows, property_name, month) <= 0:
            continue
        amount = cents(row.get("Amount"))
        if amount == 0:
            continue
        if str(row.get("BankAccountId") or "").strip() or str(row.get("Account") or "").strip():
            raise RuntimeError(
                f"cash-backed row unexpectedly matched direct-split void policy at CSV row {csv_row}"
            )
        marker_amount = cents(marker["amount"])
        if abs(abs(amount) - marker_amount) > 0.001:
            raise RuntimeError(
                f"marker/row amount mismatch at CSV row {csv_row}: marker={marker_amount} row={amount}"
            )
        transaction_id = str(row.get("BaselaneId") or "").strip()
        if not transaction_id:
            raise RuntimeError(f"missing BaselaneId at CSV row {csv_row}")
        marker = dict(marker)
        marker["property"] = property_name
        targets.append(
            {
                    "id": transaction_id,
                    "csv_row": csv_row,
                    "date": str(row.get("ISODate") or "").strip(),
                    "amount": amount,
                    "property_id": str(row.get("PropertyId") or "").strip(),
                    "tag_id": str(row.get("TagId") or "").strip(),
                    "merchant": str(row.get("Merchant") or ""),
                    "note": note,
                    "marker": marker,
            }
        )
    targets.sort(key=lambda row: (row["marker"]["property"], row["marker"]["month"], int(row["id"])))
    ids = [row["id"] for row in targets]
    if len(ids) != len(set(ids)):
        raise RuntimeError("duplicate Baselane IDs in source target set")
    return targets


def query_live_rows() -> list[dict[str, Any]]:
    query = """
    query Transactions($input: SortsAndFilters) {
      transactions(input: $input) {
        total
        data {
          id amount date merchantName propertyId tagId bankAccountId note
          isManual hidden isDeleted
        }
      }
    }
    """
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        result = run_graphql(
            {
                "operationName": "Transactions",
                "variables": {
                    "input": {
                        "sort": {"direction": "ASC", "field": "date"},
                        "filter": {
                            "search": "AOPS-",
                            "isHidden": False,
                            "isDeleted": False,
                        },
                        "page": page,
                        "pageLimit": 1000,
                    }
                },
                "query": query,
            }
        )["data"]["transactions"]
        batch = result.get("data") or []
        rows.extend(batch)
        if not batch or len(rows) >= int(result.get("total") or 0):
            return rows
        page += 1


def build_plan(targets: list[dict[str, Any]], live_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    live_by_id = {str(row.get("id") or ""): row for row in live_rows}
    plan: list[dict[str, Any]] = []
    for target in targets:
        live = live_by_id.get(target["id"])
        if not live:
            raise RuntimeError(f"missing live target {target['id']}")
        new_note = void_note(target)
        new_merchant = void_merchant(target)
        already_void = (
            cents(live.get("amount")) == 0
            and note_text(live.get("note")) == new_note
            and str(live.get("merchantName") or "") == new_merchant
        )
        if not already_void:
            actual = {
                "date": str(live.get("date") or ""),
                "amount": cents(live.get("amount")),
                "property_id": str(live.get("propertyId") or ""),
                "tag_id": str(live.get("tagId") or ""),
                "bank_account_id": str(live.get("bankAccountId") or ""),
                "is_manual": live.get("isManual"),
                "note": note_text(live.get("note")),
            }
            expected = {
                "date": target["date"],
                "amount": target["amount"],
                "property_id": target["property_id"],
                "tag_id": target["tag_id"],
                "bank_account_id": "",
                "is_manual": True,
                "note": target["note"],
            }
            if actual != expected:
                raise RuntimeError(
                    f"live identity mismatch for {target['id']}: expected={expected} actual={actual}"
                )
        plan.append(
            {
                "id": target["id"],
                "property": target["marker"]["property"],
                "month": target["marker"]["month"],
                "kind": target["marker"]["kind"],
                "old_amount": target["amount"],
                "tag_id": target["tag_id"],
                "already_void": already_void,
                "new_merchant": new_merchant,
                "new_note": new_note,
            }
        )
    return plan


def plan_digest(plan: list[dict[str, Any]]) -> str:
    bounded = [
        {
            key: row[key]
            for key in (
                "id",
                "property",
                "month",
                "kind",
                "old_amount",
                "tag_id",
                "new_merchant",
                "new_note",
            )
        }
        for row in plan
    ]
    return hashlib.sha256(
        json.dumps(bounded, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def apply_updates(plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    updates = [
        {
            "id": row["id"],
            "amount": 0.0,
            "merchantName": row["new_merchant"],
            "note": row["new_note"],
            "isReviewedByUser": True,
        }
        for row in plan
        if not row["already_void"]
    ]
    if not updates:
        return []
    return run_graphql(
        {
            "operationName": "VoidHemlaneDirectSplitPM",
            "variables": {"input": updates},
            "query": """
            mutation VoidHemlaneDirectSplitPM($input: [UpdateTransaction!]) {
              updateTransactions(input: $input) {
                id amount date merchantName propertyId tagId bankAccountId note isManual
              }
            }
            """,
        }
    )["data"]["updateTransactions"]


def verify_applied(plan: list[dict[str, Any]], live_rows: list[dict[str, Any]]) -> None:
    live_by_id = {str(row.get("id") or ""): row for row in live_rows}
    for row in plan:
        live = live_by_id.get(row["id"])
        if not live:
            raise RuntimeError(f"post-apply row missing: {row['id']}")
        if (
            cents(live.get("amount")) != 0
            or str(live.get("merchantName") or "") != row["new_merchant"]
            or note_text(live.get("note")) != row["new_note"]
            or str(live.get("tagId") or "") != row["tag_id"]
            or str(live.get("bankAccountId") or "")
            or live.get("isManual") is not True
        ):
            raise RuntimeError(f"post-apply verification failed for {row['id']}: {live}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Void legacy IL/OH/TN Baselane PM rows already settled by Hemlane direct split."
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    targets = source_targets(args.source)
    live_rows = query_live_rows()
    plan = build_plan(targets, live_rows)
    digest = plan_digest(plan)
    status = "ready"
    applied: list[dict[str, Any]] = []
    if args.apply:
        if os.environ.get(APPLY_ENV) != "1" or os.environ.get(APPLY_DIGEST_ENV) != digest:
            status = "blocked"
        else:
            applied = apply_updates(plan)
            verify_applied(plan, query_live_rows())
            status = "applied"

    correction = round(-sum(float(row["old_amount"]) for row in plan if not row["already_void"]), 2)
    report = {
        "status": status,
        "mode": "apply" if args.apply else "dry_run",
        "generated_at": iso_z(),
        "source": str(args.source),
        "reason": (
            "Hemlane already withheld/remitted IL/OH/TN PM fees before net rent reached "
            "Baselane; these accountless manual rows double-counted the same fees."
        ),
        "cash_movement": False,
        "category_or_tag_changes": False,
        "target_count": len(plan),
        "already_void_count": sum(row["already_void"] for row in plan),
        "update_count": sum(not row["already_void"] for row in plan),
        "column_e_correction": correction,
        "payload_digest": digest,
        "plan": plan,
        "applied": applied,
        "approval_command": (
            f"{APPLY_ENV}=1 {APPLY_DIGEST_ENV}={digest} "
            f"python3 scripts/{Path(__file__).name} --apply"
        ),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "status",
                    "mode",
                    "target_count",
                    "already_void_count",
                    "update_count",
                    "column_e_correction",
                    "payload_digest",
                )
            },
            indent=2,
        )
    )
    return 2 if status == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
