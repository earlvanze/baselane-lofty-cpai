#!/usr/bin/env python3
"""Reconcile unassigned DAO vendor transactions before monthly publication.

Only records backed by stable provider evidence are eligible for live
retagging. LawnStarter composite invoices are emitted as guarded native splits.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sys
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(os.environ.get("WORKSPACE_ROOT", Path(__file__).absolute().parents[1]))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "skills" / "baselane-mcp" / "src"))

from baselane_apply_monthly_accruals_live import query_transactions  # noqa: E402
from baselane_mcp.transfers import run_graphql_via_cdp  # noqa: E402


# Evidence: same vendor and amount recur in the source ledger with this exact
# property. These are single-property charges, not invoice composites.
RESOLVED = {
    # Matches the recurring 139.91 OSC premium already assigned to this
    # property on 2026-05-01, 2026-05-05, and 2026-07-03.
    "322478885": ("OSC - RISK SECURE", "-139.91", "9634 S Green St", "82374"),
    "307166823": ("OSC - RISK SECURE", "-91.17", "7411 Elton Ave", "83237"),
    "307166299": ("OSC - RISK SECURE", "-139.91", "9634 S Green St", "82374"),
    "307165619": ("OSC - RISK SECURE", "-277.99", "1456 W 85th St.", "81428"),
    "318803650": ("LAWNCARE* LAWNSTARTER", "-65.11", "5541 S Peoria St", "78900"),
    "303633034": ("LAWNCARE* LAWNSTARTER", "-65.11", "5541 S Peoria St", "78900"),
    "296786311": ("LAWNCARE* LAWNSTARTER", "-65.11", "5541 S Peoria St", "78900"),
    "291184996": ("LAWNCARE* LAWNSTARTER", "-65.11", "5541 S Peoria St", "78900"),
    "308884702": ("LAWNCARE* LAWNSTARTER", "-70.99", "7542 & 7656 S Colfax Ave", "80853"),
    "301194869": ("LAWNCARE* LAWNSTARTER", "-70.99", "7542 & 7656 S Colfax Ave", "80853"),
    "293795079": ("LAWNCARE* LAWNSTARTER", "-126.36", "5541 S Peoria St", "78900"),
    "290726187": ("LAWNCARE* LAWNSTARTER", "-219.00", "85-104 Alawa Pl", "73461"),
    "314596257": ("Hemlane", "285.30", "10724 Gooding Ave", "94511"),
    "314082801": ("Hemlane", "285.30", "10724 Gooding Ave", "94511"),
    "311501678": ("Hemlane", "1435.50", "8708 Willard Ave", "81779"),
    "294108740": ("Hemlane", "1435.50", "8708 Willard Ave", "81779"),
    "302971232": ("HEML* 9ZC5Z8:MLP", "-595.00", "724 3rd Ave", "56668"),
    "298642841": ("HEMLANE", "2750.00", "84 Madison Ave", "78308"),
    "298642323": ("Hemlane", "2750.00", "84 Madison Ave", "78308"),
    "290056547": ("Hemlane", "52.92", "8708 Willard Ave", "81779"),
    "294088233": ("HEMLANE", "765.00", "25 Circle Dr", "80460"),
    # Hemlane financial transaction codes prove these settlement components.
    "289575243": ("Hemlane", "630.00", "49 Bannbury Ln", "112365"),
    "289586310": ("Hemlane", "240.00", "49 Bannbury Ln", "112365"),
    "293520604": ("Hemlane", "293.25", "428 Cross St.", "81425"),
    "310374683": ("Hemlane", "213.30", "10724 Gooding Ave", "94511"),
    "310408468": ("Hemlane", "297.50", "428 Cross St.", "81425"),
    "310938454": ("Hemlane", "420.75", "428 Cross St.", "81425"),
    "321960239": ("Hemlane", "359.13", "428 Cross St.", "81425"),
    # Coolwood belongs to a different workspace. The exact Cross KYNB1Q
    # settlement is the only in-scope provider match for this Baselane row.
    "295207310": ("Hemlane", "425.00", "428 Cross St.", "81425"),
}
EXPECTED_TAGS = {
    "322478885": "65",  # Insurance.
    "289575243": "1",   # Rents: 700.00 less 70.00 PM fee.
    "289586310": "29",  # Security Deposits.
    "293520604": "1",
    "310374683": "1",
    "310408468": "1",
    "310938454": "1",
    "321960239": "1",
    "295207310": "1",
}
EXPECTED_DATES = {
    "322478885": "2026-07-30",
    "289575243": "2026-05-28",
    "289586310": "2026-05-28",
    "293520604": "2026-06-04",
    "310374683": "2026-07-07",
    "310408468": "2026-07-07",
    "310938454": "2026-07-08",
    "321960239": "2026-07-29",
    "295207310": "2026-06-08",
}
HEMLANE_EVIDENCE = {
    "289575243": "YXT0F1:REN 700.00 less YXT0F1:PMX 70.00",
    "289586310": "YXT7YB:DES security deposit 240.00",
    "293520604": "2KKWVM:REN 345.00 less 2KKWVM:PMX 51.75",
    "310374683": "YQLGKS:REN 237.00 less YQLGKS:PMX 23.70",
    "310408468": "2KK01Q:REN 350.00 less 2KK01Q:PMX 52.50",
    "310938454": "2KKPJ5:REN 495.00 less 2KKPJ5:PMX 74.25",
    "321960239": "2KKTRG:REN 422.50 less 2KKTRG:PMX 63.37",
    "295207310": "KYNB1Q:REN 425.00 with KYNB1Q:PMX 63.75 charge and reversal",
}
HEMLANE_COMPONENTS = {
    "289575243": (("YXT0F1:REN", 70000), ("YXT0F1:PMX", -7000)),
    "289586310": (("YXT7YB:DES", 24000),),
    "293520604": (("2KKWVM:REN", 34500), ("2KKWVM:PMX", -5175)),
    "310374683": (("YQLGKS:REN", 23700), ("YQLGKS:PMX", -2370)),
    "310408468": (("2KK01Q:REN", 35000), ("2KK01Q:PMX", -5250)),
    "310938454": (("2KKPJ5:REN", 49500), ("2KKPJ5:PMX", -7425)),
    "321960239": (("2KKTRG:REN", 42250), ("2KKTRG:PMX", -6337)),
    "295207310": (("KYNB1Q:REN", 42500), ("KYNB1Q:PMX", -6375), ("KYNB1Q:PMX", 6375)),
}
HEMLANE_PROVIDER_PROPERTIES = {
    "289575243": "49 Bannbury Ln",
    "289586310": "49 Bannbury Ln",
    "293520604": "428 Cross Street",
    "310374683": "10724 Gooding Avenue",
    "310408468": "428 Cross Street",
    "310938454": "428 Cross Street",
    "321960239": "428 Cross Street",
    "295207310": "428 Cross Street",
}
LAWNSTARTER_PROPERTIES = {
    "918 Frederick Boulevard": ("918 Frederick Blvd", "81782"),
    "8143 South Sangamon Street": ("8143 S Sangamon St.", "83181"),
    "1518 Dille Rd": ("1518 Dille Rd", "83240"),
    "7542 South Colfax Avenue": ("7542 & 7656 S Colfax Ave", "80853"),
    "428 Cross Street": ("428 Cross St.", "81425"),
    "5541 South Peoria Street": ("5541 S Peoria St", "78900"),
}
VENDOR_TERMS = ("OSC - RISK SECURE", "HEMLANE", "LAWNSTARTER")
LOCK = Path("/tmp/baselane-source-pipeline.lock")
DEFAULT_LAWNSTARTER_EVIDENCE = ROOT / "reports" / "lawnstarter_billing_evidence.json"
DEFAULT_HEMLANE_EVIDENCE = ROOT / "reports" / "hemlane_financial_evidence.json"
DEFAULT_NATIVE_SPLIT_PLAN = ROOT / "reports" / "baselane_dao_vendor_native_split_plan.json"


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@contextmanager
def pipeline_lock() -> Iterator[bool]:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def money(value: Any) -> str:
    return f"{float(value):.2f}"


def live_rows() -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for term in VENDOR_TERMS:
        for row in query_transactions(term):
            rows[str(row.get("id"))] = row
    return rows


def parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def lawnstarter_actions(
    rows: dict[str, dict[str, Any]],
    evidence_path: Path,
) -> tuple[dict[str, tuple[str, str, str, str]], dict[str, str], dict[str, str], list[dict[str, Any]], list[dict[str, Any]]]:
    assignments: dict[str, tuple[str, str, str, str]] = {}
    expected_dates: dict[str, str] = {}
    evidence_notes: dict[str, str] = {}
    split_records: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except Exception:
        return assignments, expected_dates, evidence_notes, split_records, [
            {"id": "lawnstarter_evidence", "reason": f"missing_or_invalid_evidence:{evidence_path}"}
        ]
    if evidence.get("status") != "ok" or not evidence.get("evidence_digest"):
        return assignments, expected_dates, evidence_notes, split_records, [
            {"id": "lawnstarter_evidence", "reason": "evidence_status_not_ok"}
        ]
    charges: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for charge in evidence.get("charges") or []:
        key = (str(charge.get("charge_date") or ""), f"{float(charge.get('amount') or 0):.2f}")
        charges.setdefault(key, []).append(charge)
    for transaction_id, row in rows.items():
        if "LAWNSTARTER" not in str(row.get("merchantName") or "").upper() or row.get("propertyId"):
            continue
        key = (str(row.get("date") or "")[:10], money(abs(float(row.get("amount") or 0))))
        matches = charges.get(key) or []
        if len(matches) != 1:
            failures.append({
                "id": transaction_id,
                "reason": "lawnstarter_charge_match_not_unique",
                "date": key[0],
                "amount": key[1],
                "match_count": len(matches),
            })
            continue
        charge = matches[0]
        children = []
        for allocation in charge.get("allocations") or []:
            address = str(allocation.get("property_address") or "")
            mapped = LAWNSTARTER_PROPERTIES.get(address)
            if not mapped:
                failures.append({"id": transaction_id, "reason": f"unknown_lawnstarter_address:{address}"})
                children = []
                break
            property_name, property_id = mapped
            children.append({
                "property": property_name,
                "property_id": property_id,
                "amount": f"-{float(allocation.get('amount') or 0):.2f}",
                "category": "Gardening & Landscaping",
                "tag_id": str(row.get("tagId") or "57"),
                "merchant_name": f"LawnStarter - {property_name}",
            })
        if not children:
            continue
        evidence_note = f"LawnStarter confirmation_sha256={charge['confirmation_sha256']}"
        if len(children) == 1:
            child = children[0]
            assignments[transaction_id] = (
                str(row.get("merchantName") or ""),
                money(row.get("amount")),
                child["property"],
                child["property_id"],
            )
            expected_dates[transaction_id] = key[0]
            evidence_notes[transaction_id] = evidence_note
            continue
        split_records.append({
            "id": f"lawnstarter-{transaction_id}-{charge['confirmation_sha256'][:12]}",
            "rule": "lawnstarter_provider_invoice_split",
            "status": "ready_native_split",
            "baselane_id": transaction_id,
            "date": key[0],
            "iso_date": key[0],
            "merchant": str(row.get("merchantName") or ""),
            "amount": money(row.get("amount")),
            "evidence_digest": evidence.get("evidence_digest"),
            "provider_confirmation_sha256": charge["confirmation_sha256"],
            "splits": children,
        })
    return assignments, expected_dates, evidence_notes, split_records, failures


def validate_hemlane_evidence(
    resolved: dict[str, tuple[str, str, str, str]],
    evidence_path: Path,
) -> tuple[list[dict[str, Any]], str | None]:
    required_ids = sorted(set(resolved) & set(HEMLANE_COMPONENTS))
    if not required_ids:
        return [], None
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except Exception:
        return [{"id": "hemlane_evidence", "reason": f"missing_or_invalid_evidence:{evidence_path}"}], None
    digest = str(evidence.get("evidence_digest") or "")
    if evidence.get("status") != "ok" or not digest:
        return [{"id": "hemlane_evidence", "reason": "evidence_status_not_ok"}], digest or None
    records = evidence.get("records") or []
    failures: list[dict[str, Any]] = []
    for transaction_id in required_ids:
        provider_property = HEMLANE_PROVIDER_PROPERTIES[transaction_id]
        selected: list[dict[str, Any]] = []
        for description, amount_in_cents in HEMLANE_COMPONENTS[transaction_id]:
            matches = [
                row for row in records
                if str(row.get("description") or "") == description
                and int(row.get("amount_in_cents") or 0) == amount_in_cents
                and str(row.get("property") or "") == provider_property
                and row.get("status") == "complete"
                and not row.get("is_hidden")
            ]
            if len(matches) != 1:
                failures.append({
                    "id": transaction_id,
                    "reason": "hemlane_component_match_not_unique",
                    "description": description,
                    "amount_in_cents": amount_in_cents,
                    "match_count": len(matches),
                })
                break
            selected.append(matches[0])
        if selected and sum(int(row["amount_in_cents"]) for row in selected) != round(float(resolved[transaction_id][1]) * 100):
            failures.append({"id": transaction_id, "reason": "hemlane_component_net_mismatch"})
    return failures, digest


def build_report(
    evidence_path: Path = DEFAULT_LAWNSTARTER_EVIDENCE,
    hemlane_evidence_path: Path = DEFAULT_HEMLANE_EVIDENCE,
    reporting_cutoff_date: date | None = None,
) -> dict[str, Any]:
    rows = live_rows()
    if reporting_cutoff_date is not None:
        rows = {
            transaction_id: row
            for transaction_id, row in rows.items()
            if str(row.get("date") or "")[:10] <= reporting_cutoff_date.isoformat()
        }
    dynamic, dynamic_dates, dynamic_notes, split_records, evidence_failures = lawnstarter_actions(rows, evidence_path)
    resolved = {**RESOLVED, **dynamic}
    hemlane_failures, hemlane_digest = validate_hemlane_evidence(resolved, hemlane_evidence_path)
    expected_dates = {**EXPECTED_DATES, **dynamic_dates}
    ready: list[dict[str, Any]] = []
    verified: list[dict[str, Any]] = []
    guard_failures: list[dict[str, Any]] = [*evidence_failures, *hemlane_failures]
    for transaction_id, (merchant, amount, property_name, property_id) in resolved.items():
        row = rows.get(transaction_id)
        if not row:
            guard_failures.append({"id": transaction_id, "reason": "live_transaction_missing"})
            continue
        problems = []
        current_property_id = str(row.get("propertyId") or "")
        if current_property_id and current_property_id != property_id:
            problems.append(f"property_changed:{current_property_id}")
        if money(row.get("amount")) != amount:
            problems.append(f"amount_changed:{money(row.get('amount'))}")
        if str(row.get("merchantName") or "").upper() != merchant.upper():
            problems.append("merchant_changed")
        expected_date = expected_dates.get(transaction_id)
        if expected_date and str(row.get("date") or "")[:10] != expected_date:
            problems.append(f"date_changed:{str(row.get('date') or '')[:10]}")
        expected_tag = EXPECTED_TAGS.get(transaction_id) or str(row.get("tagId") or "")
        if problems:
            guard_failures.append({"id": transaction_id, "reason": ";".join(problems), "live": row})
            continue
        if current_property_id == property_id:
            verified.append({
                "id": transaction_id,
                "merchant": merchant,
                "amount": amount,
                "property": property_name,
                "property_id": property_id,
                "tag_id": expected_tag,
                "date": row.get("date"),
                "evidence": HEMLANE_EVIDENCE.get(transaction_id) or dynamic_notes.get(transaction_id),
            })
            continue
        ready.append({
            "id": transaction_id,
            "merchant": merchant,
            "amount": amount,
            "property": property_name,
            "property_id": property_id,
            "tag_id": expected_tag,
            "date": row.get("date"),
            "evidence": HEMLANE_EVIDENCE.get(transaction_id) or dynamic_notes.get(transaction_id),
        })
    unresolved = []
    for row in rows.values():
        if str(row.get("propertyId") or ""):
            continue
        transaction_id = str(row.get("id"))
        if transaction_id in resolved or any(str(record.get("baselane_id")) == transaction_id for record in split_records):
            continue
        unresolved.append({
            "id": transaction_id,
            "date": row.get("date"),
            "merchant": row.get("merchantName"),
            "amount": money(row.get("amount")),
            "tag_id": row.get("tagId"),
            "reason": "requires_provider_invoice_or_unique_live_transaction_evidence",
        })
    unresolved.sort(key=lambda row: (str(row["date"]), str(row["id"])))
    plan = {
        "ready": ready,
        "verified": verified,
        "native_splits": split_records,
        "unresolved": unresolved,
        "guard_failures": guard_failures,
    }
    return {
        "generated_at": iso_z(),
        "status": "ok" if not guard_failures and not unresolved else "review",
        "reporting_cutoff_date": reporting_cutoff_date.isoformat() if reporting_cutoff_date else None,
        "policy": "Only stable single-property vendor evidence may update Baselane. LawnStarter composites require invoice property splits.",
        "ready_count": len(ready),
        "verified_count": len(verified),
        "unresolved_count": len(unresolved),
        "guard_failure_count": len(guard_failures),
        "native_split_count": len(split_records),
        "lawnstarter_evidence_path": str(evidence_path),
        "hemlane_evidence_path": str(hemlane_evidence_path),
        "hemlane_evidence_digest": hemlane_digest,
        "plan_digest": hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        **plan,
    }


def apply(report: dict[str, Any]) -> list[dict[str, Any]]:
    payload = {
        "operationName": "UpdateTransactions",
        "query": "mutation UpdateTransactions($input: [UpdateTransaction!]) { updateTransactions(input: $input) { id tagId propertyId merchantName amount } }",
        "variables": {"input": [
            {"id": row["id"], "tagId": row["tag_id"], "propertyId": row["property_id"], "isReviewedByUser": True}
            for row in report["ready"]
        ]},
    }
    if not payload["variables"]["input"]:
        return []
    result = run_graphql_via_cdp(payload, bridge_path=ROOT / "scripts" / "baselane_graphql_via_cdp.js", workspace_root=ROOT)
    updated = (result.get("data") or {}).get("updateTransactions") or []
    expected = {row["id"]: row["property_id"] for row in report["ready"]}
    actual = {str(row.get("id")): str(row.get("propertyId") or "") for row in updated}
    if actual != expected:
        raise RuntimeError(json.dumps({"expected": expected, "actual": actual, "result": result}, sort_keys=True))
    return updated


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--digest")
    parser.add_argument("--reporting-cutoff-date", type=parse_iso_date)
    parser.add_argument("--report", type=Path, default=ROOT / "reports" / "baselane_dao_vendor_property_reconciliation.json")
    parser.add_argument("--lawnstarter-evidence", type=Path, default=DEFAULT_LAWNSTARTER_EVIDENCE)
    parser.add_argument("--hemlane-evidence", type=Path, default=DEFAULT_HEMLANE_EVIDENCE)
    parser.add_argument("--native-split-plan", type=Path, default=DEFAULT_NATIVE_SPLIT_PLAN)
    args = parser.parse_args()
    with pipeline_lock() as acquired:
        if not acquired:
            print(json.dumps({"status": "locked", "lock": str(LOCK)}))
            return 75
        report = build_report(
            args.lawnstarter_evidence,
            args.hemlane_evidence,
            args.reporting_cutoff_date,
        )
        if args.apply:
            if args.digest != report["plan_digest"]:
                raise SystemExit("apply requires the exact current dry-run digest")
            if report["guard_failure_count"]:
                raise SystemExit("live guard failures block apply")
            report["applied"] = apply(report)
            report["applied_count"] = len(report["applied"])
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        split_plan = {
            "generated_at": report["generated_at"],
            "status": "ok",
            "source": str(args.report),
            "source_plan_digest": report["plan_digest"],
            "records": report["native_splits"],
        }
        args.native_split_plan.parent.mkdir(parents=True, exist_ok=True)
        args.native_split_plan.write_text(json.dumps(split_plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report.get(key) for key in ("status", "ready_count", "unresolved_count", "guard_failure_count", "plan_digest", "applied_count")}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
