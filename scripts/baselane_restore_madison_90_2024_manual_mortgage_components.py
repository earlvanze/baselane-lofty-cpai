#!/usr/bin/env python3
"""Restore statement-backed 90 Madison mortgage components for Jul-Oct 2024.

The original bank roots are no longer exposed by live Baselane, so these are
manual accounting components rather than native split children.  Their shared
root marker preserves the original composite payment total and makes the
workflow idempotent and independently auditable.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from baselane_apply_alcott_accruals_live import run_graphql
from baselane_web3_reconciliation_apply import create_transactions_batch


ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "reports"
CONFIG = ROOT / "config" / "madison_90_principal_curtailments.json"
DAO_PROPERTY_ID = "31525"
ECO_LEGACY_PROPERTY_ID = "37648"
MARKER = "AOPS-90-MORTGAGE-MANUAL-COMPONENT"

# Statement evidence contains only aggregate escrow for these months.  Do not
# invent tax/insurance allocations that the source does not provide.
PAYMENTS = (
    {
        "date": "2024-07-01", "root": "3300.13", "recognition": "2024-06",
        "source": "Mortgage Statement - 2024-07-01",
        "components": (
            ("ordinary-principal", "-113.53", ECO_LEGACY_PROPERTY_ID, "20"),
            ("interest", "-1655.68", ECO_LEGACY_PROPERTY_ID, "11"),
            ("escrow", "-960.92", DAO_PROPERTY_ID, "130"),
            ("returned-payment-fee", "-20.00", ECO_LEGACY_PROPERTY_ID, "109"),
            ("principal-curtailment", "-550.00", DAO_PROPERTY_ID, "20"),
        ),
    },
    {
        "date": "2024-08-06", "root": "5680.13", "recognition": "2024-07",
        "source": "Mortgage Statement - 2024-08-05; bank posted 2024-08-06",
        "components": (
            ("ordinary-principal", "-119.13", ECO_LEGACY_PROPERTY_ID, "20"),
            ("interest", "-1650.08", ECO_LEGACY_PROPERTY_ID, "11"),
            ("escrow", "-960.92", DAO_PROPERTY_ID, "130"),
            ("principal-curtailment", "-2950.00", DAO_PROPERTY_ID, "20"),
        ),
    },
    {
        "date": "2024-09-09", "root": "3880.13", "recognition": "2024-08",
        "source": "Mortgage Statement - 2024-09-09",
        "components": (
            ("ordinary-principal", "-145.02", ECO_LEGACY_PROPERTY_ID, "20"),
            ("interest", "-1624.19", ECO_LEGACY_PROPERTY_ID, "11"),
            ("escrow", "-960.92", DAO_PROPERTY_ID, "130"),
            ("principal-curtailment", "-1150.00", DAO_PROPERTY_ID, "20"),
        ),
    },
    {
        "date": "2024-10-15", "root": "3370.60", "recognition": "2024-09",
        "source": "Mortgage Statement - 2024-10-14; bank posted 2024-10-15",
        "components": (
            ("ordinary-principal", "-155.95", ECO_LEGACY_PROPERTY_ID, "20"),
            ("interest", "-1613.26", ECO_LEGACY_PROPERTY_ID, "11"),
            ("escrow", "-801.39", DAO_PROPERTY_ID, "130"),
            ("principal-curtailment", "-800.00", DAO_PROPERTY_ID, "20"),
        ),
    },
)


def money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def note_text(value: Any) -> str:
    return str(value.get("text") or "") if isinstance(value, dict) else str(value or "")


def targets() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for payment in PAYMENTS:
        root_total = sum((money(row[1]) for row in payment["components"]), Decimal("0.00"))
        if root_total != -money(payment["root"]):
            raise RuntimeError(f"components do not sum for {payment['date']}: {root_total}")
        for component, amount, property_id, tag_id in payment["components"]:
            key = f"{payment['date']}|{payment['root']}|{component}"
            note = (
                f"{MARKER}|root-date={payment['date']}|root-amount={payment['root']}|"
                f"component={component}|recognition={payment['recognition']} | "
                f"Statement-backed reconstruction; no new cash movement. Source: {payment['source']}."
            )
            result.append({
                "key": key,
                "identity": (
                    f"root-date={payment['date']}|root-amount={payment['root']}|"
                    f"component={component}"
                ),
                "component": component,
                "recognition": payment["recognition"],
                "values": {
                    "merchantName": f"90 Madison | {component.replace('-', ' ')} | {payment['date']}",
                    "note": note,
                    "tagId": tag_id,
                    "propertyId": property_id,
                    "unitId": None,
                    "entityId": None,
                    "date": payment["date"],
                    "bankAccountId": None,
                    "amount": float(money(amount)),
                    "isReviewedByUser": True,
                },
            })
    return result


def query_marker_rows() -> list[dict[str, Any]]:
    query = """
    query Transactions($input: SortsAndFilters) {
      transactions(input: $input) { total data {
        id amount date merchantName propertyId tagId bankAccountId note
        isManual hidden isDeleted
      } }
    }
    """
    found: dict[str, dict[str, Any]] = {}
    # Baselane's text search is merchant-oriented and is not dependable for
    # note markers. Query both involved properties and filter notes locally.
    for property_id in (DAO_PROPERTY_ID, ECO_LEGACY_PROPERTY_ID):
        for hidden in (False, True):
            for deleted in (False, True):
                payload = {
                    "operationName": "Transactions",
                    "variables": {"input": {
                        "sort": {"direction": "ASC", "field": "date"},
                        "filter": {"propertyId": property_id, "isHidden": hidden, "isDeleted": deleted},
                        "page": 1, "pageLimit": 1000,
                    }},
                    "query": query,
                }
                for row in run_graphql(payload)["data"]["transactions"].get("data") or []:
                    if MARKER in note_text(row.get("note")):
                        found[str(row["id"])] = row
    return list(found.values())


def exact(target: dict[str, Any], row: dict[str, Any]) -> bool:
    wanted = target["values"]
    base_exact = (
        not row.get("hidden") and not row.get("isDeleted")
        and money(row.get("amount")) == money(wanted["amount"])
        and str(row.get("date") or "")[:10] == wanted["date"]
        and str(row.get("propertyId") or "") == wanted["propertyId"]
        and str(row.get("tagId") or "") == wanted["tagId"]
    )
    if not base_exact:
        return False
    if target["component"] == "principal-curtailment":
        # The canonical normalizer prepends AOPS-90-CURTAILMENT and renames
        # the row, while retaining this script's composite-root identity.
        return target["identity"] in note_text(row.get("note"))
    return (
        str(row.get("merchantName") or "") == wanted["merchantName"]
        and note_text(row.get("note")) == wanted["note"]
    )


def digest(plan: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def update_config(mapping: dict[str, str]) -> None:
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    for row in data["recognition_schedule"]:
        month = row["month"]
        if month in mapping:
            row["status"] = "mapped"
            row["transaction_id"] = mapping[month]
            row.pop("bank_root_id", None)
    CONFIG.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-digest")
    args = parser.parse_args()

    wanted = targets()
    live = query_marker_rows()
    actions: list[dict[str, Any]] = []
    for target in wanted:
        matches = [row for row in live if target["identity"] in note_text(row.get("note"))]
        if any(exact(target, row) for row in matches):
            action = "none"
        elif matches:
            action = "conflict"
        else:
            action = "create"
        actions.append({"key": target["key"], "action": action, "values": target["values"]})
    plan = {"marker": MARKER, "actions": actions}
    plan_digest = digest(plan)
    if args.apply and args.expected_digest != plan_digest:
        raise RuntimeError(f"digest mismatch: expected {args.expected_digest!r}, live plan {plan_digest}")
    conflicts = [row for row in actions if row["action"] == "conflict"]
    if conflicts:
        raise RuntimeError(f"conflicting marker rows: {json.dumps(conflicts, indent=2)}")

    created: list[dict[str, Any]] = []
    if args.apply:
        creates = [row["values"] for row in actions if row["action"] == "create"]
        for offset in range(0, len(creates), 5):
            created.extend(create_transactions_batch(creates[offset:offset + 5]))

        verified = query_marker_rows()
        missing = [target["key"] for target in wanted if not any(exact(target, row) for row in verified)]
        if missing:
            raise RuntimeError(f"post-apply verification failed: {missing}")
        mapping: dict[str, str] = {}
        for target in wanted:
            if target["component"] != "principal-curtailment":
                continue
            row = next(row for row in verified if exact(target, row))
            mapping[target["recognition"]] = str(row["id"])
        update_config(mapping)

    report = {
        "generated_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "status": "applied" if args.apply else "preview",
        "digest": plan_digest,
        "create_count": sum(row["action"] == "create" for row in actions),
        "actions": actions,
        "created": created,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    output = REPORT_DIR / "madison_90_2024_manual_mortgage_components.json"
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(output), "status": report["status"], "digest": plan_digest,
                      "create_count": report["create_count"], "created_ids": [str(row["id"]) for row in created]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
