#!/usr/bin/env python3
"""Guarded, idempotent transfer of validated DAO-held bank interest to ECO."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any


WORKSPACE = Path(__file__).absolute().parents[1]
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "baselane-mcp" / "src"))

from baselane_mcp.transfers import (  # noqa: E402
    build_transfer_plan,
    execute_transfer,
    list_active_transfer_accounts,
)
from baselane_settle_madison_pm_mortgage import (  # noqa: E402
    cents,
    graphql,
    note_text,
    query_parent,
    query_recent_transfer_rows,
    update_parent_metadata,
)


REPORT = WORKSPACE / "reports" / "baselane_live_dao_cash_reconciliation.json"
STATE = ROOT / "reports" / "baselane_interest_transfer_state.json"
DRY_RUN = ROOT / "reports" / "baselane_interest_transfer_dry_run.json"
APPLIED = ROOT / "reports" / "baselane_interest_transfer_applied.json"
AUDIT_ONLY = ROOT / "reports" / "baselane_interest_transfer_audit_only.json"
ECO_TRANSFER_ACCOUNT_ID = 29732
TAG_ID = "24"
MARKER = "ECO bank interest through June 2026"
CENT = Decimal("0.01")

PROPERTY_IDS = {
    "1278 E 187th St": "93597",
    "1518 Dille Rd": "83240",
    "22164 Umland Circle": "83184",
    "254 Bowmanville St": "83241",
    "326 South Alcott Street": "77356",
    "428 Cross St.": "81425",
    "566 Nash St": "96348",
    "724 3rd Ave": "33594",
    "804 S Quitman St": "57369",
    "8143 S Sangamon St.": "83181",
    "84 Madison Ave": "60548",
    "86 Madison Ave": "63162",
    "9 Country Club Ln N": "91341",
    "90 Madison Ave": "31525",
    "9902 Garfield Ave": "86933",
    "9919 S Oglesby Ave": "80590",
}


def money(value: Any) -> Decimal:
    return Decimal(str(value or "0")).quantize(CENT)


def alphanumeric(value: str) -> str:
    return " ".join("".join(ch if ch.isalnum() else " " for ch in value).split())


def load_reconciliation() -> dict[str, Any]:
    payload = json.loads(REPORT.read_text(encoding="utf-8"))
    if payload.get("status") != "ok":
        raise RuntimeError("cash reconciliation report is not status=ok")
    if payload.get("issues"):
        raise RuntimeError(f"cash reconciliation has issues: {payload['issues']}")
    if payload.get("unmapped_property_dao_accounts"):
        raise RuntimeError("cash reconciliation contains unmapped DAO accounts")
    return payload


def completed_confirmation_tokens() -> set[str]:
    if not STATE.is_file():
        return set()
    payload = json.loads(STATE.read_text(encoding="utf-8"))
    transfers = payload.get("transfers") or {}
    return {
        str(token)
        for token, record in transfers.items()
        if isinstance(record, dict) and record.get("status") == "completed"
    }


def principal_floor(account: dict[str, Any]) -> Decimal:
    evidence = account.get("savings_evidence") or {}
    values = [
        money(evidence.get("documented_security_principal")),
        money(evidence.get("documented_reserve_principal")),
    ]
    return max(values)


def build_specs(
    report: dict[str, Any],
    *,
    selected_properties: set[str] | None,
    completed_tokens: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]], list[str]]:
    completed_tokens = completed_tokens or set()
    live_rows = list_active_transfer_accounts(graphql)
    live = {int(row["transfer_account_id"]): row for row in live_rows}
    issues: list[str] = []
    specs: list[dict[str, Any]] = []

    if ECO_TRANSFER_ACCOUNT_ID not in live:
        issues.append("ECO 2624 is unavailable for internal transfer")

    report_names = {str(row["property"]) for row in report["properties"]}
    missing_ids = sorted(
        row["property"]
        for row in report["properties"]
        if money(row.get("validated_interest_cash_transfer")) > 0
        and row["property"] not in PROPERTY_IDS
    )
    if missing_ids:
        issues.append(f"missing Baselane property IDs: {missing_ids}")
    if selected_properties:
        unknown = sorted(selected_properties - report_names)
        if unknown:
            issues.append(f"unknown selected properties: {unknown}")

    for prop in report["properties"]:
        property_name = str(prop["property"])
        if selected_properties and property_name not in selected_properties:
            continue
        expected_total = money(prop.get("validated_interest_cash_transfer"))
        if expected_total <= 0:
            continue
        if property_name not in PROPERTY_IDS:
            continue

        account_rows = {
            int(row["transfer_account_id"]): row for row in prop.get("accounts") or []
        }
        live_property_total = sum(
            (
                money(live[transfer_id]["available_balance"])
                for transfer_id in account_rows
                if transfer_id in live
            ),
            Decimal("0"),
        )
        property_post = live_property_total - expected_total
        protected = money(prop.get("protected_minimum"))
        if prop.get("active") and live_property_total < protected:
            issues.append(
                f"{property_name} is already below its protected minimum; no outflow allowed"
            )
        elif prop.get("active") and property_post < protected:
            issues.append(
                f"{property_name} would fall below protected minimum {protected:.2f}"
            )

        source_total = Decimal("0")
        for source in prop.get("interest_transfer_sources") or []:
            amount = money(source["amount"])
            source_total += amount
            transfer_id = int(source["transfer_account_id"])
            report_account = account_rows.get(transfer_id)
            live_account = live.get(transfer_id)
            if not report_account or not live_account:
                issues.append(
                    f"{property_name} source account {transfer_id} is unavailable"
                )
                continue
            live_balance = money(live_account["available_balance"])
            floor = principal_floor(report_account)
            note = alphanumeric(
                f"{MARKER} {property_name} source {transfer_id}"
            )
            label = (
                f"ECO interest | {property_name} | through 2026-06 | "
                f"{source['nickname']}"
            )
            plan = build_transfer_plan(
                from_transfer_account_id=transfer_id,
                to_transfer_account_id=ECO_TRANSFER_ACCOUNT_ID,
                amount=amount,
                bookkeeping_note=note,
                property_id=PROPERTY_IDS[property_name],
                tag_id=TAG_ID,
                same_day=True,
            )
            if plan["confirmation_token"] in completed_tokens:
                continue
            if live_balance - amount < floor:
                issues.append(
                    f"{property_name} source {transfer_id} would breach principal "
                    f"floor {floor:.2f}"
                )
            specs.append(
                {
                    "property": property_name,
                    "property_id": PROPERTY_IDS[property_name],
                    "active": bool(prop.get("active")),
                    "source_kind": source["source"],
                    "source_nickname": source["nickname"],
                    "source_transfer_account_id": transfer_id,
                    "source_bank_account_id": str(live_account["bank_account_id"]),
                    "source_opening_balance": format(live_balance, ".2f"),
                    "source_principal_floor": format(floor, ".2f"),
                    "amount": format(amount, ".2f"),
                    "label": label,
                    "bookkeeping_note": note,
                    "plan": plan,
                }
            )
        if source_total != expected_total:
            issues.append(
                f"{property_name} source total {source_total:.2f} does not match "
                f"validated transfer {expected_total:.2f}"
            )

    return specs, live, issues


def public_plan(
    specs: list[dict[str, Any]],
    live: dict[int, dict[str, Any]],
    issues: list[str],
    report: dict[str, Any],
) -> dict[str, Any]:
    total = sum((money(row["amount"]) for row in specs), Decimal("0"))
    return {
        "status": "dry_run",
        "source_reconciliation_as_of": report["as_of"],
        "policy": {
            "internal_only": True,
            "destination": "ECO 2624",
            "destination_transfer_account_id": ECO_TRANSFER_ACCOUNT_ID,
            "tag_id": TAG_ID,
            "category": "Transfers Between Accounts",
            "period": "through 2026-06",
            "one_transfer_per_physical_source_account": True,
            "security_and_reserve_principal_preserved": True,
            "active_property_protected_minimum_preserved": True,
        },
        "issues": issues,
        "transfer_count": len(specs),
        "total": format(total, ".2f"),
        "eco_opening_available_balance": (
            format(
                money(live[ECO_TRANSFER_ACCOUNT_ID]["available_balance"]),
                ".2f",
            )
            if ECO_TRANSFER_ACCOUNT_ID in live
            else None
        ),
        "transfers": [
            {
                key: spec[key]
                for key in (
                    "property",
                    "property_id",
                    "active",
                    "source_kind",
                    "source_nickname",
                    "source_transfer_account_id",
                    "source_bank_account_id",
                    "source_opening_balance",
                    "source_principal_floor",
                    "amount",
                    "label",
                    "bookkeeping_note",
                )
            }
            | {
                "confirmation_token": spec["plan"]["confirmation_token"],
            }
            for spec in specs
        ],
    }


def plan_digest(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def ensure_audit(
    spec: dict[str, Any],
    *,
    destination_bank_account_id: str,
) -> dict[str, Any]:
    expected_bank_ids = {
        str(spec["source_bank_account_id"]),
        str(destination_bank_account_id),
    }
    exact: list[dict[str, Any]] = []
    for attempt in range(4):
        rows = [
            row
            for bank_id in sorted(expected_bank_ids)
            for row in query_recent_transfer_rows(bank_id)
        ]
        exact = [
            row
            for row in rows
            if (
                not row.get("isDeleted")
                and not row.get("parentId")
                and str(row.get("bankAccountId") or "") in expected_bank_ids
                and str(row.get("date") or "") == spec["plan"]["transfer_date"]
                and abs(cents(row.get("amount") or 0)) == money(spec["amount"])
                and note_text(row.get("note"))
                in {spec["bookkeeping_note"], spec["label"]}
            )
        ]
        if len(exact) == 2 and {
            str(row.get("bankAccountId")) for row in exact
        } == expected_bank_ids:
            break
        if attempt < 3:
            time.sleep(2)
    if len(exact) != 2:
        return {
            "status": "pending_bank_mirrors",
            "matched_parent_ids": [str(row.get("id")) for row in exact],
        }

    parent_ids = [str(row["id"]) for row in exact]
    update_parent_metadata(
        parent_ids,
        label=spec["label"],
        property_id=spec["property_id"],
        note=spec["bookkeeping_note"],
    )
    verified = [query_parent(parent_id) for parent_id in parent_ids]
    if any(
        (
            str(row.get("tagId")) != TAG_ID
            or str(row.get("propertyId")) != spec["property_id"]
            or str(row.get("merchantName")) != spec["label"]
            or note_text(row.get("note")) != spec["bookkeeping_note"]
            or abs(cents(row.get("amount") or 0)) != money(spec["amount"])
        )
        for row in verified
    ):
        raise RuntimeError("interest-transfer mirror metadata verification failed")
    return {
        "status": "verified_mirrored_parents",
        "parent_ids": parent_ids,
        "all_rows_tag_id": TAG_ID,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--digest")
    parser.add_argument(
        "--property",
        action="append",
        help="Limit to an exact property name from the reconciliation report.",
    )
    args = parser.parse_args()
    if args.apply and args.audit_only:
        parser.error("--apply and --audit-only are mutually exclusive")

    selected = set(args.property or []) or None
    report = load_reconciliation()
    specs, live, issues = build_specs(
        report,
        selected_properties=selected,
        # Audit mode must retain completed plan rows so their two mirrored
        # bank transactions can be found and verified. Apply/dry-run mode
        # continues to omit completed tokens idempotently.
        completed_tokens=(
            set() if args.audit_only else completed_confirmation_tokens()
        ),
    )
    public = public_plan(specs, live, issues, report)
    digest = plan_digest(public)
    dry = {"digest": digest, **public}
    write_json(DRY_RUN, dry)

    destination_bank_id = str(
        live.get(ECO_TRANSFER_ACCOUNT_ID, {}).get("bank_account_id") or ""
    )
    if args.audit_only:
        audit = {
            "status": "existing_cash_rows_audited",
            "rows": [
                {"property": spec["property"], **ensure_audit(
                    spec,
                    destination_bank_account_id=destination_bank_id,
                )}
                for spec in specs
            ],
        }
        write_json(AUDIT_ONLY, audit)
        print(json.dumps({**audit, "report": str(AUDIT_ONLY)}, indent=2))
        return 0

    if not args.apply:
        print(json.dumps({**dry, "report": str(DRY_RUN)}, indent=2))
        return 0 if not issues else 2
    if args.digest != digest:
        raise RuntimeError(f"live digest is {digest}; exact --digest required")
    if issues:
        raise RuntimeError(f"refusing apply with issues: {issues}")

    receipts: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    for spec in specs:
        result = execute_transfer(
            plan=spec["plan"],
            confirmation_token=spec["plan"]["confirmation_token"],
            graphql_runner=graphql,
            state_path=STATE,
        )
        receipts.append({"property": spec["property"], **result})
        audits.append(
            {
                "property": spec["property"],
                **ensure_audit(
                    spec,
                    destination_bank_account_id=destination_bank_id,
                ),
            }
        )
    ending = list_active_transfer_accounts(graphql)
    ending_by_id = {
        int(row["transfer_account_id"]): row for row in ending
    }
    applied = {
        "status": "submitted_and_verified",
        "digest": digest,
        **public,
        "eco_ending_available_balance": format(
            money(ending_by_id[ECO_TRANSFER_ACCOUNT_ID]["available_balance"]),
            ".2f",
        ),
        "receipts": receipts,
        "audits": audits,
    }
    write_json(APPLIED, applied)
    print(json.dumps({**applied, "report": str(APPLIED)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
