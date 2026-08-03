#!/usr/bin/env python3
"""Build an ID-bearing no-DAO-mortgage liability waterfall (read only)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("OPENCLAW_WORKSPACE_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "scripts"))
from baselane_alawa_loandepot_cleanup import run_graphql  # noqa: E402

DEFAULT_CONFIG = ROOT / "config" / "no_dao_mortgage_liability_reconciliation.json"
DEFAULT_REPORT = ROOT / "reports" / "no_dao_mortgage_liability_reconciliation.json"
COMPONENT_TAGS = {
    "principal": "20", "interest": "11", "escrow_tax": "15",
    "escrow_insurance": "8", "escrow_general": "130", "eco_fee": "24",
}


def money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def note_text(value: Any) -> str:
    return str(value.get("text") or "") if isinstance(value, dict) else str(value or "")


def query_id(transaction_id: str) -> dict[str, Any] | None:
    return run_graphql({
        "operationName": "TransactionById", "variables": {"id": transaction_id},
        "query": """query TransactionById($id: ID!) { transactionById(id: $id) {
          id amount date propertyId tagId bankAccountId merchantName note isDeleted hidden
          splitTransactions { id amount date propertyId tagId bankAccountId merchantName isDeleted }
        }}""",
    })["data"].get("transactionById")


def load_offline(path: Path) -> dict[str, dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    rows = raw.get("transactions", raw) if isinstance(raw, dict) else raw
    if isinstance(rows, dict):
        return {str(key): value for key, value in rows.items()}
    return {str(row["id"]): row for row in rows}


def expected_total(event: dict[str, Any]) -> Decimal:
    return sum((money(value) for value in event["components"].values()), Decimal("0.00"))


def aggregate_live_components(row: dict[str, Any]) -> dict[str, Decimal]:
    totals = {key: Decimal("0.00") for key in COMPONENT_TAGS}
    reverse = {tag: key for key, tag in COMPONENT_TAGS.items()}
    for child in row.get("splitTransactions") or []:
        if child.get("isDeleted"):
            continue
        key = reverse.get(str(child.get("tagId") or ""))
        if key:
            totals[key] += abs(money(child.get("amount")))
    return totals


def build_report(config: dict[str, Any], property_name: str, rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    policy = config["policy"]
    item = config["properties"][property_name]
    blockers: list[dict[str, Any]] = []
    split_differences: list[dict[str, Any]] = []
    payments: list[dict[str, Any]] = []
    gross_pi = gross_fees = restricted_escrow = Decimal("0.00")

    for event in item["payment_events"]:
        row = rows.get(str(event["parent_id"]))
        expected = expected_total(event)
        if not row or row.get("isDeleted") or row.get("hidden"):
            blockers.append({"id": event["parent_id"], "reason": "missing_or_inactive_payment"})
            continue
        identity_errors = []
        if str(row.get("bankAccountId") or "") not in set(item["dao_bank_account_ids"]):
            identity_errors.append("unexpected_bank_account")
        if str(row.get("date") or "") != event["date"]:
            identity_errors.append("date_mismatch")
        if abs(money(row.get("amount"))) != expected:
            identity_errors.append("amount_mismatch")
        if identity_errors:
            blockers.append({"id": event["parent_id"], "reason": identity_errors})
            continue
        components = {key: money(value) for key, value in event["components"].items()}
        pi = components.get("principal", Decimal(0)) + components.get("interest", Decimal(0))
        fees = components.get("eco_fee", Decimal(0))
        escrow = expected - pi - fees
        gross_pi += pi
        gross_fees += fees
        restricted_escrow += escrow
        live = aggregate_live_components(row)
        differences = {
            key: {"expected": str(value), "live": str(live.get(key, Decimal(0)))}
            for key, value in components.items()
            if live.get(key, Decimal(0)) != value
        }
        if differences:
            split_differences.append({"id": event["parent_id"], "months": event["installment_months"], "differences": differences})
        payments.append({
            "id": event["parent_id"], "date": event["date"],
            "installment_months": event["installment_months"],
            "principal_and_interest_due_from_eco": str(pi),
            "lender_fees_due_from_eco": str(fees),
            "restricted_dao_escrow": str(escrow), "cash_total": str(expected),
            "source": event["source"],
        })

    confirmed: list[dict[str, Any]] = []
    reimbursed = Decimal("0.00")
    for settlement in item.get("confirmed_reimbursements", []):
        row = rows.get(str(settlement["id"]))
        if not row or row.get("isDeleted") or row.get("hidden"):
            blockers.append({"id": settlement["id"], "reason": "missing_or_inactive_reimbursement"})
            continue
        mismatches = []
        if money(row.get("amount")) != money(settlement["amount"]): mismatches.append("amount")
        if str(row.get("date") or "") != settlement["date"]: mismatches.append("date")
        if settlement["memo"].lower() not in note_text(row.get("note")).lower(): mismatches.append("memo")
        if settlement.get("native_split_merchant"):
            matching_children = [
                child for child in row.get("splitTransactions") or []
                if not child.get("isDeleted")
                and str(child.get("merchantName") or "") == settlement["native_split_merchant"]
                and str(child.get("tagId") or "") == "24"
                and money(child.get("amount")) == money(settlement["native_split_amount"])
            ]
            if len(matching_children) != 1:
                mismatches.append("native_split")
        if mismatches:
            blockers.append({"id": settlement["id"], "reason": "reimbursement_identity_mismatch", "fields": mismatches})
            continue
        applied = money(settlement["applied_to_eco_responsibility"])
        reimbursed += applied
        confirmed.append({**settlement, "unallocated_transfer_remainder": str(money(settlement["amount"]) - applied)})

    candidates = []
    for candidate in item.get("candidate_reimbursements", []):
        row = rows.get(str(candidate["id"]))
        status = "present_unallocated" if row and not row.get("isDeleted") and not row.get("hidden") else "missing_or_inactive"
        candidates.append({**candidate, "status": status})

    gross_responsibility = gross_pi + gross_fees
    open_mortgage = gross_responsibility - reimbursed
    other_ap = {key: money(value) for key, value in item.get("other_open_dao_payables_to_eco", {}).items()}
    dao_ap_to_eco = sum(other_ap.values(), Decimal("0.00"))
    net_due_to_dao = open_mortgage - dao_ap_to_eco
    canonical = {
        "property": property_name, "cash_cutoff": policy["cash_cutoff"],
        "gross_pi_due_from_eco": str(gross_pi), "gross_lender_fees_due_from_eco": str(gross_fees),
        "confirmed_eco_reimbursements": str(reimbursed), "open_mortgage_due_from_eco": str(open_mortgage),
        "restricted_dao_escrow_paid": str(restricted_escrow),
        "other_dao_ap_to_eco": {key: str(value) for key, value in other_ap.items()},
        "net_after_explicit_cross_entity_ap": str(net_due_to_dao),
    }
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {
        "schema_version": 1, "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "status": "blocked" if blockers else ("review" if candidates or split_differences else "ok"),
        "mode": "read_only", "policy": policy, "property": property_name,
        "source_digest": digest, "summary": canonical, "payments": payments,
        "confirmed_reimbursements": confirmed, "candidate_reimbursements": candidates,
        "native_split_differences": split_differences, "blockers": blockers,
        "publication_guard": "Do not publish or move cash while status is blocked; candidates never reduce the balance without an approved allocation.",
        "investor_disclaimer": "If anything looks wrong, please DM @earlvanze on Discord or email ecosystemspm@gmail.com.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--property", default="85-104 Alawa Pl")
    scope.add_argument("--all-configured", action="store_true")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--offline-transactions", type=Path)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    property_names = list(config["properties"]) if args.all_configured else [args.property]
    ids = []
    for property_name in property_names:
        item = config["properties"][property_name]
        ids.extend(row["parent_id"] for row in item["payment_events"])
        ids.extend(
            row["id"]
            for key in ("confirmed_reimbursements", "candidate_reimbursements")
            for row in item.get(key, [])
        )
    ids = list(dict.fromkeys(ids))
    rows = load_offline(args.offline_transactions) if args.offline_transactions else {str(i): query_id(str(i)) for i in ids}
    property_reports = {
        property_name: build_report(config, property_name, rows)
        for property_name in property_names
    }
    if len(property_reports) == 1:
        report = next(iter(property_reports.values()))
    else:
        statuses = {item["status"] for item in property_reports.values()}
        status = "blocked" if "blocked" in statuses else ("review" if "review" in statuses else "ok")
        digest_payload = {
            name: item["source_digest"] for name, item in sorted(property_reports.items())
        }
        report = {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status,
            "mode": "read_only",
            "scope": "all_configured",
            "source_digest": hashlib.sha256(
                json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "property_count": len(property_reports),
            "properties": property_reports,
            "publication_guard": "Every property must be reviewed independently; a blocked property forbids its publication or cash movement.",
        }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if len(property_reports) == 1:
        console = {
            "status": report["status"], "source_digest": report["source_digest"],
            "summary": report["summary"],
            "split_difference_count": len(report["native_split_differences"]),
            "candidate_count": len(report["candidate_reimbursements"]),
            "blocker_count": len(report["blockers"]), "report": str(args.report),
        }
    else:
        console = {
            "status": report["status"], "source_digest": report["source_digest"],
            "property_count": report["property_count"],
            "property_statuses": {name: item["status"] for name, item in property_reports.items()},
            "report": str(args.report),
        }
    print(json.dumps(console, indent=2))
    return 1 if report["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
