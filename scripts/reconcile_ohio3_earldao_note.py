#!/usr/bin/env python3
"""Idempotently reconcile Ohio 3's EARLDAO loan and interest subledger.

The 2025 governance record recognizes a $19,680.80 economic loan from
EARLDAO to Lofty Holding 1518 Dille Road DAO LLC.  That legacy draw assumed
the DAO's Yhome-related deficit and therefore was not a Baselane cash
transfer.  Later $351, $419.15, and $1,000 advances did move through bank
accounts and remain internal-transfer cash rows.

This workflow:

* creates the paired non-cash legacy principal recognition;
* creates paired monthly simple-interest accounting rows through 2026-07-29;
* labels the verified cash advances and the $900 interest payment;
* allocates $419.15 of the existing 2026-07-24 EARLDAO-funded ECO clearing
  advance to the Dille plumbing advance; and
* leaves all actual cash-transfer parents and children in category 24.

It never initiates a cash transfer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from baselane_apply_alcott_accruals_live import run_graphql
from baselane_live_dao_cash_reconciliation import graphql, note_text
from baselane_settle_madison_pm_mortgage import (
    cents,
    normalized_children,
    query_parent,
    reconcile_parent_split,
)
from baselane_mcp.transfers import list_active_transfer_accounts


ROOT = Path("/home/digit/.openclaw/workspace")
REPORT_DIR = ROOT / "reports"
AS_OF = date(2026, 7, 29)

OHIO3_PROPERTY_ID = "83240"
ALLENDALE_PROPERTY_ID = "83239"
EARLDAO_PROPERTY_ID = "94512"

TAG_INTEREST_EXPENSE = "12"
TAG_INTEREST_REVENUE = "2"
TAG_PRINCIPAL_PAYMENT = "22"
TAG_LOAN_PROCEEDS = "23"
TAG_TRANSFER = "24"

LEGACY_PRINCIPAL = Decimal("19680.80")
INTEREST_RATE = Decimal("0.12")
INTEREST_PAYMENT = Decimal("900.00")

DRAWS = [
    (date(2025, 5, 27), LEGACY_PRINCIPAL, "approved legacy deficit assumption"),
    (date(2025, 11, 13), Decimal("351.00"), "Dille short-term cash advance"),
    (date(2026, 3, 9), Decimal("419.15"), "Dille plumbing advance via ECO agent"),
    (date(2026, 7, 1), Decimal("1000.00"), "Dille Formica plumbing advance"),
]

SOURCE_ROWS = {
    "209775770": ("351.00", "2025-11-13", "117761"),
    "209775777": ("-351.00", "2025-11-13", "131545"),
    "221199427": ("900.00", "2025-12-17", "131545"),
    "221199430": ("-900.00", "2025-12-17", "131496"),
    "251379001": ("419.15", "2026-03-09", "117761"),
    "251379003": ("-419.15", "2026-03-09", "114283"),
    "307137169": ("1000.00", "2026-07-01", "117761"),
    "307137173": ("-1000.00", "2026-07-01", "131545"),
    "319772383": ("-10000.00", "2026-07-24", "131545"),
    "319772392": ("10000.00", "2026-07-24", "38968"),
    "321403938": ("-5724.64", "2026-07-28", ""),
    "321403944": ("-7131.92", "2026-01-16", ""),
}

MARKER_PREFIX = "OHIO3-EARLDAO|"


def money(value: Any) -> str:
    return f"{Decimal(str(value or 0)).quantize(Decimal('0.01')):.2f}"


def query_source_rows() -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    ids = list(SOURCE_ROWS)
    for offset in range(0, len(ids), 5):
        batch = ids[offset : offset + 5]
        fields = "\n".join(
            f"""
            t{index}: transactionById(id: "{transaction_id}") {{
              id amount date merchantName propertyId tagId bankAccountId note
              isManual isDeleted isSplit parentId
              splitTransactions {{
                id amount date merchantName propertyId tagId parentId isDeleted
              }}
            }}
            """
            for index, transaction_id in enumerate(batch)
        )
        data = graphql(
            {
                "operationName": "Ohio3EarldaoSourceRows",
                "variables": {},
                "query": f"query Ohio3EarldaoSourceRows {{ {fields} }}",
            }
        )["data"]
        rows.update(
            {str(row["id"]): row for row in data.values() if row}
        )
    return rows


def query_marker_rows() -> list[dict[str, Any]]:
    result = run_graphql(
        {
            "operationName": "Transactions",
            "variables": {
                "input": {
                    "sort": {"direction": "ASC", "field": "date"},
                    "filter": {
                        "search": MARKER_PREFIX,
                        "isHidden": False,
                        "isDeleted": False,
                    },
                    "page": 1,
                    "pageLimit": 250,
                }
            },
            "query": """
            query Transactions($input: SortsAndFilters) {
              transactions(input: $input) {
                data {
                  id amount date merchantName propertyId tagId bankAccountId
                  note isManual isDeleted
                }
              }
            }
            """,
        }
    )["data"]["transactions"]["data"]
    return [row for row in result if MARKER_PREFIX in note_text(row.get("note"))]


def live_balances() -> dict[str, str]:
    wanted = {
        "1321 Allendale Ave Operations": "Allendale",
        "1518 Dille Rd Operations": "Dille",
        "Ohio-3 Security Deposits": "Ohio3 security deposits",
        "EARLDAO Operations": "EARLDAO",
    }
    found: dict[str, str] = {}
    for row in list_active_transfer_accounts(graphql):
        nickname = str(row.get("nickname") or "")
        if nickname in wanted:
            found[wanted[nickname]] = money(row.get("available_balance"))
    return found


def interest_total() -> Decimal:
    raw = sum(
        (
            principal
            * INTEREST_RATE
            * Decimal((AS_OF - draw_date).days)
            / Decimal("365")
            for draw_date, principal, _ in DRAWS
        ),
        Decimal("0"),
    )
    return raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def interest_buckets() -> list[tuple[str, date, Decimal]]:
    buckets: list[tuple[str, date, Decimal]] = []
    cursor = date(2025, 5, 1)
    while cursor < AS_OF:
        if cursor.month == 12:
            next_month = date(cursor.year + 1, 1, 1)
        else:
            next_month = date(cursor.year, cursor.month + 1, 1)
        stop = min(next_month, AS_OF)
        raw = sum(
            (
                principal
                * INTEREST_RATE
                * Decimal((stop - max(cursor, draw_date)).days)
                / Decimal("365")
                for draw_date, principal, _ in DRAWS
                if draw_date < stop
            ),
            Decimal("0"),
        )
        amount = raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        buckets.append((cursor.strftime("%Y-%m"), stop - timedelta(days=1), amount))
        cursor = next_month
    rounding_delta = interest_total() - sum(
        (amount for _, _, amount in buckets), Decimal("0")
    )
    month, row_date, amount = buckets[-1]
    buckets[-1] = (month, row_date, amount + rounding_delta)
    return buckets


def accounting_targets() -> list[dict[str, Any]]:
    targets = [
        {
            "marker": f"{MARKER_PREFIX}legacy-principal|borrower",
            "date": "2025-05-27",
            "amount": LEGACY_PRINCIPAL,
            "merchantName": "EARLDAO loan draw | Ohio 3 legacy deficit",
            "propertyId": OHIO3_PROPERTY_ID,
            "tagId": TAG_LOAN_PROCEEDS,
            "note": (
                f"{MARKER_PREFIX}legacy-principal|borrower | Non-cash recognition "
                "of the $19,680.80 EARLDAO economic loan approved 2025-05-27. "
                "The facility assumed Ohio 3's legacy Yhome/operating deficit; "
                "it is not a Baselane bank deposit."
            ),
        },
        {
            "marker": f"{MARKER_PREFIX}legacy-principal|lender",
            "date": "2025-05-27",
            "amount": -LEGACY_PRINCIPAL,
            "merchantName": "Ohio 3 loan receivable | EARLDAO",
            "propertyId": EARLDAO_PROPERTY_ID,
            "tagId": TAG_PRINCIPAL_PAYMENT,
            "note": (
                f"{MARKER_PREFIX}legacy-principal|lender | EARLDAO receivable "
                "matching the $19,680.80 non-cash Ohio 3 legacy-deficit loan "
                "recognized under the 2025-05-27 approval."
            ),
        },
    ]
    for month, row_date, amount in interest_buckets():
        common = (
            f"12% simple Actual/365 interest for {month}; includes each verified "
            "draw only from its funding/recognition date. No compounding."
        )
        targets.extend(
            [
                {
                    "marker": f"{MARKER_PREFIX}interest|{month}|borrower",
                    "date": row_date.isoformat(),
                    "amount": -amount,
                    "merchantName": f"EARLDAO interest accrual | Ohio 3 | {month}",
                    "propertyId": OHIO3_PROPERTY_ID,
                    "tagId": TAG_INTEREST_EXPENSE,
                    "note": (
                        f"{MARKER_PREFIX}interest|{month}|borrower | {common}"
                    ),
                },
                {
                    "marker": f"{MARKER_PREFIX}interest|{month}|lender",
                    "date": row_date.isoformat(),
                    "amount": amount,
                    "merchantName": f"Ohio 3 interest receivable | EARLDAO | {month}",
                    "propertyId": EARLDAO_PROPERTY_ID,
                    "tagId": TAG_INTEREST_REVENUE,
                    "note": (
                        f"{MARKER_PREFIX}interest|{month}|lender | {common}"
                    ),
                },
            ]
        )
    return targets


def source_updates() -> list[dict[str, Any]]:
    labels = {
        "209775770": "EARLDAO -> Ohio 3 | principal draw | 2025-11-13 | $351",
        "209775777": "EARLDAO -> Ohio 3 | principal draw | 2025-11-13 | $351",
        "221199427": "Ohio 3 -> EARLDAO | interest payment | 2025-12-17 | $900",
        "221199430": "Ohio 3 -> EARLDAO | interest payment | 2025-12-17 | $900",
        "251379001": "EARLDAO -> Ohio 3 via ECO agent | plumbing | $419.15",
        "251379003": "EARLDAO -> Ohio 3 via ECO agent | plumbing | $419.15",
        "307137169": "EARLDAO -> Ohio 3 | post-maturity plumbing draw | $1,000",
        "307137173": "EARLDAO -> Ohio 3 | post-maturity plumbing draw | $1,000",
    }
    updates = []
    for transaction_id, label in labels.items():
        updates.append(
            {
                "id": transaction_id,
                "merchantName": label,
                "propertyId": OHIO3_PROPERTY_ID,
                "tagId": TAG_TRANSFER,
                "note": (
                    f"{MARKER_PREFIX}cash-subledger|{transaction_id} | {label}. "
                    "Actual internal cash transfer; category remains Transfers "
                    "Between Accounts. Included in Ohio 3/EARLDAO principal or "
                    "interest subledger as labeled."
                ),
            }
        )
    return updates


def desired_clearing_children(parent: dict[str, Any]) -> list[dict[str, Any]]:
    sign = Decimal("-1") if cents(parent["amount"]) < 0 else Decimal("1")
    components = [
        ("final ECO bridge repayment", Decimal("3000.00"), EARLDAO_PROPERTY_ID),
        (
            "Ohio 3 plumbing advance via ECO agent",
            Decimal("419.15"),
            OHIO3_PROPERTY_ID,
        ),
        (
            "clearing advance pending DAO allocation",
            Decimal("6580.85"),
            EARLDAO_PROPERTY_ID,
        ),
    ]
    children = [
        {
            "amount": cents(sign * amount),
            "date": str(parent["date"]),
            "merchantName": f"EARLDAO -> ECO | {label} | ${amount:.2f}",
            "propertyId": property_id,
            "tagId": TAG_TRANSFER,
        }
        for label, amount, property_id in components
    ]
    if sum((row["amount"] for row in children), Decimal("0")) != cents(
        parent["amount"]
    ):
        raise RuntimeError("clearing split components do not sum to parent")
    return children


def validate_sources(rows: dict[str, dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    for transaction_id, expected in SOURCE_ROWS.items():
        row = rows.get(transaction_id)
        if not row:
            issues.append(f"missing source transaction {transaction_id}")
            continue
        actual = (
            money(row.get("amount")),
            str(row.get("date") or "")[:10],
            str(row.get("bankAccountId") or ""),
            bool(row.get("isDeleted")),
        )
        wanted = (*expected, False)
        if actual != wanted:
            issues.append(
                f"source transaction {transaction_id} changed: "
                f"expected {wanted}, found {actual}"
            )
    return issues


def target_values(target: dict[str, Any]) -> dict[str, Any]:
    return {
        "merchantName": target["merchantName"],
        "note": target["note"],
        "tagId": target["tagId"],
        "propertyId": target["propertyId"],
        "unitId": None,
        "entityId": None,
        "date": target["date"],
        "bankAccountId": None,
        "amount": float(target["amount"]),
        "isReviewedByUser": True,
    }


def create_transaction(target: dict[str, Any]) -> dict[str, Any]:
    return run_graphql(
        {
            "operationName": "createTransaction",
            "variables": target_values(target),
            "query": """
            mutation createTransaction(
              $merchantName: String!, $note: String!, $tagId: ID,
              $propertyId: ID, $unitId: ID, $entityId: Int, $date: String!,
              $bankAccountId: ID, $amount: Float!,
              $isReviewedByUser: Boolean
            ) {
              createTransaction(input: {
                merchantName: $merchantName note: $note tagId: $tagId
                propertyId: $propertyId unitId: $unitId entityId: $entityId
                date: $date bankAccountId: $bankAccountId amount: $amount
                isReviewedByUser: $isReviewedByUser
              }) {
                id amount date merchantName propertyId tagId bankAccountId
                note isManual
              }
            }
            """,
        }
    )["data"]["createTransaction"]


def update_rows(updates: list[dict[str, Any]]) -> None:
    result = graphql(
        {
            "operationName": "UpdateOhio3EarldaoRows",
            "variables": {"input": updates},
            "query": """
            mutation UpdateOhio3EarldaoRows($input: [UpdateTransaction!]) {
              updateTransactions(input: $input) {
                id merchantName propertyId tagId note
              }
            }
            """,
        }
    )["data"]["updateTransactions"]
    if {str(row["id"]) for row in result} != {
        str(row["id"]) for row in updates
    }:
        raise RuntimeError("Baselane did not confirm every source-row update")


def marker_map(rows: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    found: dict[str, dict[str, Any]] = {}
    issues: list[str] = []
    for row in rows:
        text = note_text(row.get("note"))
        if f"{MARKER_PREFIX}cash-subledger|" in text:
            continue
        matches = [
            target["marker"]
            for target in accounting_targets()
            if target["marker"] in text
        ]
        if len(matches) != 1:
            issues.append(f"unrecognized/ambiguous managed row {row.get('id')}")
            continue
        marker = matches[0]
        if marker in found:
            issues.append(f"duplicate managed marker {marker}")
        found[marker] = row
    return found, issues


def target_matches(target: dict[str, Any], row: dict[str, Any]) -> bool:
    return (
        money(row.get("amount")) == money(target["amount"])
        and str(row.get("date") or "")[:10] == target["date"]
        and str(row.get("merchantName") or "") == target["merchantName"]
        and str(row.get("propertyId") or "") == target["propertyId"]
        and str(row.get("tagId") or "") == target["tagId"]
        and row.get("bankAccountId") is None
        and bool(row.get("isManual"))
        and note_text(row.get("note")) == target["note"]
    )


def build_plan() -> dict[str, Any]:
    sources = query_source_rows()
    markers, marker_issues = marker_map(query_marker_rows())
    issues = validate_sources(sources) + marker_issues
    actions = []
    for target in accounting_targets():
        current = markers.get(target["marker"])
        if current and target_matches(target, current):
            action = "skip"
        elif current:
            action = "update"
        else:
            action = "create"
        actions.append(
            {
                "action": action,
                "existing_id": str(current["id"]) if current else None,
                **{key: money(value) if key == "amount" else value
                   for key, value in target.items()},
            }
        )

    gross_interest = interest_total()
    gross_principal = sum((amount for _, amount, _ in DRAWS), Decimal("0"))
    balances = live_balances()
    ohio3_cash = sum(
        (
            Decimal(balances.get(name, "0"))
            for name in ("Allendale", "Dille", "Ohio3 security deposits")
        ),
        Decimal("0"),
    )
    return {
        "scope": "Ohio 3 package / EARLDAO principal and interest reconciliation",
        "as_of": AS_OF.isoformat(),
        "issues": issues,
        "cash_movement_created": False,
        "governance": {
            "approval_date": "2025-05-27",
            "application_id": "3020381930",
            "recognized_legacy_loan": "19680.80",
            "terms": "12% APR simple; interest-only; one-year balloon; non-dilutive",
            "legacy_maturity": "2026-05-27",
        },
        "principal": {
            "legacy_noncash_deficit_assumption": "19680.80",
            "later_cash_advances": [
                {"date": draw_date.isoformat(), "amount": money(amount), "basis": basis}
                for draw_date, amount, basis in DRAWS[1:]
            ],
            "gross_principal": money(gross_principal),
            "principal_payments": "0.00",
            "ending_principal": money(gross_principal),
            "renewal_exception": (
                "The $1,000 advance on 2026-07-01 postdates the original "
                "one-year maturity and requires renewal/ratification."
            ),
        },
        "interest": {
            "basis": "12% simple Actual/365; no compounding",
            "monthly": [
                {"month": month, "date": row_date.isoformat(), "amount": money(amount)}
                for month, row_date, amount in interest_buckets()
            ],
            "gross_accrued": money(gross_interest),
            "cash_paid_2025_12_17": money(INTEREST_PAYMENT),
            "unpaid": money(gross_interest - INTEREST_PAYMENT),
        },
        "total_debt": money(gross_principal + gross_interest - INTEREST_PAYMENT),
        "web3_yhome_components": {
            "Allendale": "5724.64",
            "Lily": "7131.92",
            "Dille": "1564.28",
            "total": "14420.84",
            "treatment": (
                "Existing Web3/Yhome settlement use-of-funds rows remain "
                "separate from the EARLDAO loan draw recognition."
            ),
        },
        "lily_sale": {
            "baselane_closing_cash_2026_01_16": "6408.28",
            "seller_note_original_principal": "50000.00",
            "seller_note_rate": "7%",
            "seller_note_monthly_payment": "332.65",
            "seller_note_balloon": "2031-01",
            "conclusion": "No full EARLDAO payoff occurred at the Lily closing.",
        },
        "live_cash": {
            **balances,
            "Ohio3 total": money(ohio3_cash),
            "required_coownership_floor": "3000.00",
            "cash_transfer_due_now": "0.00",
            "reason": "Ohio 3 is below its $3,000 coownership reserve floor.",
        },
        "accounting_actions": actions,
        "cash_source_updates": source_updates(),
        "clearing_reallocation": {
            "2026_07_24_original_unallocated": "7000.00",
            "allocated_to_ohio3": "419.15",
            "remaining_unallocated": "6580.85",
            "parent_ids": ["319772383", "319772392"],
            "cash_tags_remain_24": True,
        },
        "source_row_ids": sorted(SOURCE_ROWS, key=int),
    }


def digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def write_report(name: str, payload: dict[str, Any]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / name
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def apply_accounting_targets(plan: dict[str, Any]) -> None:
    current, _ = marker_map(query_marker_rows())
    for target in accounting_targets():
        row = current.get(target["marker"])
        if row and target_matches(target, row):
            continue
        if row:
            update = {
                "id": str(row["id"]),
                "amount": float(target["amount"]),
                "merchantName": target["merchantName"],
                "note": target["note"],
                "tagId": target["tagId"],
                "propertyId": target["propertyId"],
                "unitId": None,
                "isReviewedByUser": True,
            }
            update_rows([update])
        else:
            create_transaction(target)


def verify() -> dict[str, Any]:
    plan = build_plan()
    issues = list(plan["issues"])
    if any(row["action"] != "skip" for row in plan["accounting_actions"]):
        issues.append("managed accounting rows do not match target state")
    sources = query_source_rows()
    expected_updates = {str(row["id"]): row for row in source_updates()}
    for transaction_id, expected in expected_updates.items():
        actual = sources.get(transaction_id)
        if not actual:
            continue
        for field in ("merchantName", "propertyId", "tagId"):
            if str(actual.get(field) or "") != str(expected[field]):
                issues.append(f"{transaction_id} {field} is not reconciled")
    for parent_id in ("319772383", "319772392"):
        parent = sources.get(parent_id)
        if parent and normalized_children(
            parent.get("splitTransactions") or []
        ) != normalized_children(desired_clearing_children(parent)):
            issues.append(f"{parent_id} clearing split does not match")
    return {
        **plan,
        "status": "verified" if not issues else "verification_failed",
        "issues": issues,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--digest")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()

    if args.verify:
        report = verify()
        path = write_report(
            "ohio3_earldao_note_reconciliation.20260729.json", report
        )
        print(json.dumps({**report, "report": str(path)}, indent=2))
        return 0 if report["status"] == "verified" else 2

    plan = build_plan()
    plan_digest = digest(plan)
    dry = {"status": "dry_run", "digest": plan_digest, **plan}
    path = write_report("ohio3_earldao_note_reconciliation.dry_run.json", dry)
    if not args.apply:
        print(json.dumps({**dry, "report": str(path)}, indent=2))
        return 0 if not plan["issues"] else 2
    if args.digest != plan_digest:
        raise RuntimeError(f"live digest is {plan_digest}; exact --digest required")
    if plan["issues"]:
        raise RuntimeError(f"refusing apply with issues: {plan['issues']}")

    apply_accounting_targets(plan)
    update_rows(source_updates())
    for parent_id in ("319772383", "319772392"):
        parent = query_parent(parent_id)
        target = desired_clearing_children(parent)
        if normalized_children(
            parent.get("splitTransactions") or []
        ) != normalized_children(target):
            reconcile_parent_split(parent, target)

    report = verify()
    if report["status"] != "verified":
        raise RuntimeError(f"post-apply verification failed: {report['issues']}")
    report["status"] = "applied_and_verified"
    report["digest"] = plan_digest
    path = write_report(
        "ohio3_earldao_note_reconciliation.20260729.json", report
    )
    print(json.dumps({**report, "report": str(path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
