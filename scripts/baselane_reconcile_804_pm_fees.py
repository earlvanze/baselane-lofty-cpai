#!/usr/bin/env python3
"""Backfill paired 804 S Quitman PM-fee accruals through March 2026.

This is a retained manual-only reconciliation. It recognizes a 13% PM fee on
ten ID-bearing gross-rent receipts from June 2025 through March 2026. The DAO
expense and ECO revenue are accounting-only reciprocal rows; this script never
moves cash and does not treat an unlabeled transfer as a fee settlement.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from baselane_apply_alcott_accruals_live import run_graphql  # noqa: E402


PROPERTY = "804 S Quitman St"
PROPERTY_ID = "57369"
ECO_PROPERTY_ID = "37648"
PM_RATE = Decimal("0.13")
TAG_PROPERTY_MANAGEMENT = "80"
TAG_FEES_OTHER_REVENUE = "2"
MARKER_PREFIX = "AOPS-804-PM-FEE"
PIPELINE_LOCK = ROOT / "scripts" / ".baselane_source_pipeline.lock"
DEFAULT_SOURCE_INDEX = ROOT / "reports" / "baselane_source_transaction_index.csv"
DEFAULT_REPORT = ROOT / "reports" / "baselane_804_pm_fee_reconciliation.json"

# The service month can differ from the bank date. In particular, June 2025
# rent arrived on 2025-05-30 and March 2026 rent arrived on 2026-04-20.
RENT_EVIDENCE: tuple[dict[str, Any], ...] = (
    {"month": "2025-06", "gross": "3200.00", "primary_id": "279683562", "receipt_date": "2025-05-30"},
    {"month": "2025-07", "gross": "3500.00", "primary_id": "172076190", "receipt_date": "2025-07-07"},
    {"month": "2025-08", "gross": "3500.00", "primary_id": "185616979", "receipt_date": "2025-08-27"},
    {"month": "2025-09", "gross": "3650.00", "primary_id": "191382480", "receipt_date": "2025-09-16"},
    {"month": "2025-10", "gross": "3500.00", "primary_id": "198133277", "receipt_date": "2025-10-08"},
    {"month": "2025-11", "gross": "3675.00", "primary_id": "211970106", "receipt_date": "2025-11-20"},
    {"month": "2025-12", "gross": "2742.00", "primary_id": "221216415", "receipt_date": "2025-12-18"},
    {"month": "2026-01", "gross": "3675.00", "primary_id": "234935837", "receipt_date": "2026-01-26"},
    {"month": "2026-02", "gross": "3675.00", "primary_id": "244388851", "receipt_date": "2026-02-20"},
    {
        "month": "2026-03",
        "gross": "3500.00",
        "primary_id": "271498865",
        "receipt_date": "2026-04-20",
        "corroborating_ids": ("274510087", "274510117"),
    },
)


def money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def pm_fee(gross: Any) -> Decimal:
    return money(money(gross) * PM_RATE)


def posting_date(month: str) -> str:
    return f"{month}-28"


def marker(kind: str, month: str) -> str:
    return f"{MARKER_PREFIX}|{kind}|{PROPERTY}|{month}"


def note_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or "")
    return str(value or "")


def targets() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for evidence in RENT_EVIDENCE:
        month = str(evidence["month"])
        gross = money(evidence["gross"])
        fee = pm_fee(gross)
        evidence_ids = [str(evidence["primary_id"]), *map(str, evidence.get("corroborating_ids") or ())]
        basis = (
            f"13% of ${gross:.2f} gross rent for {month}; source Baselane transaction(s) "
            f"{', '.join(evidence_ids)}. Receipt date {evidence['receipt_date']}. "
            "Accounting/manual accrual only; no cash movement. Any cash settlement must be recorded separately."
        )
        dao_marker = marker("pm_dao", month)
        eco_marker = marker("pm_eco", month)
        result.extend((
            {
                "marker": dao_marker,
                "month": month,
                "side": "dao_expense",
                "merchantName": f"PM Fee Accrual | {PROPERTY} | {month}",
                "note": f"{dao_marker} | DAO-side Property Management expense; matched to ECO revenue. {basis}",
                "tagId": TAG_PROPERTY_MANAGEMENT,
                "propertyId": PROPERTY_ID,
                "date": posting_date(month),
                "amount": str(-fee),
            },
            {
                "marker": eco_marker,
                "month": month,
                "side": "eco_revenue",
                "merchantName": f"ECO Systems LLC PM Fee Revenue | {PROPERTY} | {month}",
                "note": f"{eco_marker} | ECO Systems LLC Fees & Other Revenue; matched to DAO expense. {basis}",
                "tagId": TAG_FEES_OTHER_REVENUE,
                "propertyId": ECO_PROPERTY_ID,
                "date": posting_date(month),
                "amount": str(fee),
            },
        ))
    return result


def all_evidence_ids() -> list[str]:
    result: list[str] = []
    for row in RENT_EVIDENCE:
        result.append(str(row["primary_id"]))
        result.extend(map(str, row.get("corroborating_ids") or ()))
    return result


def query_evidence() -> dict[str, dict[str, Any] | None]:
    ids = all_evidence_ids()
    fields = "id amount date propertyId tagId bankAccountId merchantName note isManual isDeleted hidden"
    query = "query Verify804RentEvidence {\n" + "\n".join(
        f'r{index}: transactionById(id: {json.dumps(tx_id)}) {{ {fields} }}'
        for index, tx_id in enumerate(ids)
    ) + "\n}"
    data = run_graphql({"operationName": "Verify804RentEvidence", "variables": {}, "query": query})["data"]
    return {tx_id: data.get(f"r{index}") for index, tx_id in enumerate(ids)}


def query_marker_rows() -> list[dict[str, Any]]:
    result = run_graphql({
        "operationName": "Transactions",
        "variables": {"input": {
            "sort": {"direction": "DESC", "field": "date"},
            "filter": {"search": MARKER_PREFIX, "isHidden": False, "isDeleted": False},
            "page": 1,
            "pageLimit": 100,
        }},
        "query": """
        query Transactions($input: SortsAndFilters) {
          transactions(input: $input) {
            total
            data { id amount date propertyId tagId bankAccountId merchantName note isManual isDeleted hidden }
          }
        }
        """,
    })["data"]["transactions"]
    if int(result.get("total") or 0) > 100:
        raise RuntimeError("unexpected marker population exceeds bounded query")
    return result.get("data") or []


def live_matches(target: dict[str, Any], row: dict[str, Any]) -> bool:
    return all((
        money(row.get("amount") or 0) == money(target["amount"]),
        str(row.get("date") or "") == target["date"],
        str(row.get("propertyId") or "") == target["propertyId"],
        str(row.get("tagId") or "") == target["tagId"],
        str(row.get("merchantName") or "") == target["merchantName"],
        note_text(row.get("note")) == target["note"],
        row.get("bankAccountId") is None,
        bool(row.get("isManual")),
        not row.get("isDeleted"),
        not row.get("hidden"),
    ))


def evidence_blockers(evidence_rows: dict[str, dict[str, Any] | None]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for evidence in RENT_EVIDENCE:
        tx_id = str(evidence["primary_id"])
        row = evidence_rows.get(tx_id)
        expected = {
            "amount": money(evidence["gross"]),
            "date": str(evidence["receipt_date"]),
            "propertyId": PROPERTY_ID,
        }
        if not row:
            blockers.append({"id": tx_id, "month": evidence["month"], "reason": "missing_primary_rent_evidence"})
            continue
        mismatches: dict[str, Any] = {}
        for field, value in expected.items():
            live_value = money(row.get(field) or 0) if field == "amount" else str(row.get(field) or "")
            if live_value != value:
                mismatches[field] = {"expected": str(value), "live": str(live_value)}
        if row.get("isDeleted") or row.get("hidden"):
            mismatches["active"] = {"expected": True, "live": False}
        if mismatches:
            blockers.append({"id": tx_id, "month": evidence["month"], "reason": "primary_rent_identity_mismatch", "mismatches": mismatches})

        for corroborating_id in map(str, evidence.get("corroborating_ids") or ()):
            corroborating = evidence_rows.get(corroborating_id)
            if not corroborating or corroborating.get("isDeleted") or corroborating.get("hidden"):
                blockers.append({"id": corroborating_id, "month": evidence["month"], "reason": "missing_or_inactive_corroborating_evidence"})
    return blockers


def build_plan() -> dict[str, Any]:
    evidence_rows = query_evidence()
    blockers = evidence_blockers(evidence_rows)
    by_marker: dict[str, list[dict[str, Any]]] = {}
    for row in query_marker_rows():
        prefix = note_text(row.get("note")).split(" | ", 1)[0]
        if prefix.startswith(MARKER_PREFIX):
            by_marker.setdefault(prefix, []).append(row)

    actions: list[dict[str, Any]] = []
    for target in targets():
        rows = by_marker.get(target["marker"]) or []
        if len(rows) > 1:
            blockers.append({"marker": target["marker"], "reason": "duplicate_live_marker", "ids": [row.get("id") for row in rows]})
        elif not rows:
            actions.append({"action": "create", "target": target})
        elif live_matches(target, rows[0]):
            actions.append({"action": "skip", "reason": "already_current", "id": rows[0].get("id"), "target": target})
        else:
            blockers.append({"marker": target["marker"], "reason": "manual_identity_mismatch", "live": rows[0], "target": target})
    return {"actions": actions, "blockers": blockers, "evidence": evidence_rows}


def plan_digest(plan: dict[str, Any]) -> str:
    payload = {
        "creates": [row["target"] for row in plan["actions"] if row["action"] == "create"],
        "blockers": plan["blockers"],
        "evidence_ids": all_evidence_ids(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def create_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    created: list[dict[str, Any]] = []
    for offset in range(0, len(rows), 5):
        batch = rows[offset:offset + 5]
        fields: list[str] = []
        for index, row in enumerate(batch):
            fields.append(
                f"r{index}: createTransaction(input: {{"
                f"merchantName: {json.dumps(row['merchantName'])} note: {json.dumps(row['note'])} "
                f"tagId: {json.dumps(row['tagId'])} propertyId: {json.dumps(row['propertyId'])} "
                "unitId: null entityId: null bankAccountId: null "
                f"date: {json.dumps(row['date'])} amount: {row['amount']} isReviewedByUser: true"
                "}) { id amount date propertyId tagId bankAccountId note isManual }"
            )
        data = run_graphql({
            "operationName": "Create804PmFeeAccruals",
            "variables": {},
            "query": "mutation Create804PmFeeAccruals {\n" + "\n".join(fields) + "\n}",
        })["data"]
        created.extend(data[f"r{index}"] for index in range(len(batch)))
    return created


def offline_source_review(path: Path) -> dict[str, Any]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    by_id = {str(row.get("BaselaneId") or ""): row for row in rows}
    evidence_rows: dict[str, dict[str, Any] | None] = {}
    for tx_id in all_evidence_ids():
        source = by_id.get(tx_id)
        evidence_rows[tx_id] = None if source is None else {
            "id": tx_id,
            "amount": source.get("Amount"),
            "date": source.get("ISODate"),
            "propertyId": source.get("PropertyId"),
            "tagId": source.get("TagId"),
            "bankAccountId": source.get("BankAccountId") or None,
            "merchantName": source.get("Merchant"),
            "note": source.get("Notes"),
            "isManual": source.get("TagIdSource") == "MANUAL",
            "isDeleted": False,
            "hidden": False,
        }
    markers = [row for row in rows if MARKER_PREFIX in str(row.get("Notes") or "")]
    return {
        "status": "offline_review",
        "source_index": str(path),
        "source_evidence_blockers": evidence_blockers(evidence_rows),
        "source_marker_count": len(markers),
        "target_count": len(targets()),
        "gross_rent_total": str(sum((money(row["gross"]) for row in RENT_EVIDENCE), Decimal("0.00"))),
        "pm_fee_total": str(sum((pm_fee(row["gross"]) for row in RENT_EVIDENCE), Decimal("0.00"))),
        "schedule": [
            {**row, "pm_fee": str(pm_fee(row["gross"]))}
            for row in RENT_EVIDENCE
        ],
        "note": "Offline evidence only. Rerun against live Baselane to obtain an authoritative mutation digest.",
    }


def mutation_lock():
    PIPELINE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    return PIPELINE_LOCK.open("a+", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--require-plan-digest")
    parser.add_argument("--offline-source-index", type=Path)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)

    if args.offline_source_index:
        if args.apply:
            parser.error("--offline-source-index cannot be combined with --apply")
        report = offline_source_review(args.offline_source_index)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 0 if not report["source_evidence_blockers"] else 2

    plan = build_plan()
    digest = plan_digest(plan)
    applied: list[dict[str, Any]] = []
    verify: dict[str, Any] | None = None
    if args.apply:
        if not args.require_plan_digest:
            parser.error("--apply requires --require-plan-digest")
        with mutation_lock() as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            plan = build_plan()
            digest = plan_digest(plan)
            if digest != args.require_plan_digest:
                raise SystemExit(f"plan digest changed: expected {args.require_plan_digest}, current {digest}")
            if plan["blockers"]:
                raise SystemExit(f"live blockers prevent apply: {json.dumps(plan['blockers'], indent=2, default=str)}")
            applied = create_rows([row["target"] for row in plan["actions"] if row["action"] == "create"])
            verify = build_plan()

    remaining = [] if verify is None else [row for row in verify["actions"] if row["action"] == "create"]
    status = "blocked" if plan["blockers"] else "needs_apply"
    if verify is not None:
        status = "ok" if not verify["blockers"] and not remaining else "failed"
    elif not any(row["action"] == "create" for row in plan["actions"]):
        status = "ok"
    report = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "status": status,
        "mode": "apply" if args.apply else "dry_run",
        "property": PROPERTY,
        "rate": str(PM_RATE),
        "gross_rent_total": str(sum((money(row["gross"]) for row in RENT_EVIDENCE), Decimal("0.00"))),
        "pm_fee_total": str(sum((pm_fee(row["gross"]) for row in RENT_EVIDENCE), Decimal("0.00"))),
        "plan_digest": digest,
        "plan": plan,
        "applied": applied,
        "verify": verify,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": status,
        "mode": report["mode"],
        "plan_digest": digest,
        "create_count": sum(row["action"] == "create" for row in plan["actions"]),
        "skip_count": sum(row["action"] == "skip" for row in plan["actions"]),
        "blocker_count": len(plan["blockers"]),
        "gross_rent_total": report["gross_rent_total"],
        "pm_fee_total": report["pm_fee_total"],
        "report": str(args.report),
    }, indent=2))
    return 0 if status in {"ok", "needs_apply"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
