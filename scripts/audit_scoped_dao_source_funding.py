#!/usr/bin/env python3
"""Audit scoped 2026 bank funding and produce the manual settlement schedule."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from baselane_reconciliation_policy import is_non_cash_close_row


ROOT = Path(__file__).absolute().parents[1]
DEFAULT_SOURCE = ROOT / "reports" / "baselane_source_transaction_index.csv"
DEFAULT_CAPITAL_MODEL = ROOT / "reports" / "scoped_dao_2026_capital_model.json"
DEFAULT_ECO_ALLOCATION_REPORT = ROOT / "reports" / "scoped_dao_live_eco_allocation_20260714.json"
REPORT_STEM = ROOT / "reports" / "scoped_dao_source_funding_audit_20260714"
MONEY = Decimal("0.01")

PROPERTY_OWNERS = {
    "84 Madison Ave": "Lychee LFTY0431 DAO LLC",
    "86 Madison Ave": "Snow Leopard LFTY0439 DAO LLC",
    "88 Madison Ave": "Heron LFTY0314 DAO LLC",
    "90 Madison Ave": "Strawberry LFTY402 DAO LLC",
    "724 3rd Ave": "Grape LFTY403 DAO LLC",
    "85-104 Alawa Pl": "Poodle LFTY0452 DAO LLC",
    "9 Country Club Ln N": "Zebra LFTY0476 DAO LLC",
    "27 Pillar Ln": "Kiwi LFTY400 DAO LLC",
    "22164 Umland Circle": "Beagle LFTY0454 DAO LLC",
    "122 Florida Park Dr": "Goose LFTY0320 DAO LLC",
    "15555 Millard Ave": "Lofty Holding 15555 Millard Avenue DAO LLC",
    "5541 S Peoria St": "LOFTY HOLDING 5541 S PEORIA STREET DAO LLC",
}

CORE_ENTITIES = {
    "ECO Systems LLC",
    "Lychee LFTY0431 DAO LLC",
    "Snow Leopard LFTY0439 DAO LLC",
    "Heron LFTY0314 DAO LLC",
    "Strawberry LFTY402 DAO LLC",
    "Grape LFTY403 DAO LLC",
    "Poodle LFTY0452 DAO LLC",
}

DAO_PREFIXES = {
    "Lychee LFTY0431 DAO LLC": "84 Madison Ave",
    "Snow Leopard LFTY0439 DAO LLC": "86 Madison Ave",
    "Heron LFTY0314 DAO LLC": "88 Madison Ave",
    "Strawberry LFTY402 DAO LLC": "90 Madison Ave",
    "Grape LFTY403 DAO LLC": "724 3rd Ave",
    "Poodle LFTY0452 DAO LLC": "85-104 Alawa Pl",
}

SCOPED_PROPERTIES = set(DAO_PREFIXES.values())
CORE_DAOS = set(DAO_PREFIXES)


def money(value: str | float | Decimal) -> Decimal:
    return Decimal(str(value or 0)).quantize(MONEY)


def account_owner(account: str) -> str:
    if account.startswith("ECO Systems, LLC-"):
        return "ECO Systems LLC"
    match = re.match(r"^(.+? DAO LLC)-", account, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    if account.startswith("Checking-Checking-0000-0000"):
        return "Earl Co (personal Chase checking ...0000)"
    return f"Account owner unresolved: {account}"


def property_owner(property_name: str) -> str:
    return PROPERTY_OWNERS.get(property_name, f"Property ledger: {property_name or '[missing property]'}")


def load_rows(path: Path, cutoff: date) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    return [
        row for row in rows
        if row.get("ISODate", "").startswith("2026-")
        and date.fromisoformat(row["ISODate"]) <= cutoff
    ]


def net_pairwise(obligations: dict[tuple[str, str], Decimal]) -> list[dict[str, object]]:
    entities = sorted({entity for pair in obligations for entity in pair})
    output: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for left in entities:
        for right in entities:
            if left == right or tuple(sorted((left, right))) in seen:
                continue
            seen.add(tuple(sorted((left, right))))
            net = obligations.get((left, right), Decimal(0)) - obligations.get((right, left), Decimal(0))
            if abs(net) < MONEY:
                continue
            debtor, creditor, amount = (left, right, net) if net > 0 else (right, left, -net)
            if debtor not in CORE_ENTITIES and creditor not in CORE_ENTITIES:
                continue
            output.append({
                "debtor": debtor,
                "creditor": creditor,
                "amount": float(amount.quantize(MONEY)),
                "scope": "core" if debtor in CORE_ENTITIES and creditor in CORE_ENTITIES else "external",
            })
    return sorted(output, key=lambda row: (-Decimal(str(row["amount"])), str(row["debtor"]), str(row["creditor"])))


def write_csv(path: Path, rows: list[dict[str, object]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--cutoff", type=date.fromisoformat, default=date(2026, 7, 14))
    parser.add_argument("--report-stem", type=Path, default=REPORT_STEM)
    parser.add_argument("--capital-model", type=Path, default=DEFAULT_CAPITAL_MODEL)
    parser.add_argument("--eco-allocation-report", type=Path, default=DEFAULT_ECO_ALLOCATION_REPORT)
    args = parser.parse_args()

    rows = load_rows(args.source, args.cutoff)
    obligations: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    obligation_evidence: list[dict[str, object]] = []
    missing_tags: list[dict[str, str]] = []
    suspicious_non_property: list[dict[str, str]] = []
    actual_scoped_outflows = 0
    actual_scoped_outflow_total = Decimal(0)
    eco_paid: dict[str, Decimal] = defaultdict(Decimal)
    software_sources: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    shed_rows: list[dict[str, str]] = []
    pm_fee_accrued: dict[str, Decimal] = defaultdict(Decimal)
    pm_fee_paid: dict[str, Decimal] = defaultdict(Decimal)
    no_dao_mortgage_pi_cash: dict[str, Decimal] = defaultdict(Decimal)
    eco_allocated_cash_net: dict[str, Decimal] = {}
    non_cash_close_rows: list[dict[str, str]] = []

    for row in rows:
        if is_non_cash_close_row(row):
            non_cash_close_rows.append(row)
            continue
        account = row.get("Account", "").strip()
        amount = money(row.get("Amount", "0"))
        prop = row.get("Property", "").strip()
        category = row.get("Category", "").strip()
        owner = account_owner(account) if account else ""

        if owner in DAO_PREFIXES and amount < 0:
            actual_scoped_outflows += 1
            actual_scoped_outflow_total += -amount
            if not prop or not category or not row.get("TagId", "").strip() or not row.get("PropertyId", "").strip():
                missing_tags.append(row)
            if category == "Non-Property Expense":
                suspicious_non_property.append(row)

        if account and owner == "ECO Systems LLC" and prop in SCOPED_PROPERTIES and amount < 0:
            eco_paid[prop] += -amount

        merchant_text = " ".join((row.get("Merchant", ""), row.get("Description", ""))).lower()
        if (
            account and owner in CORE_DAOS and amount < 0 and category == "Software Subscriptions"
            and ("pricelabs" in merchant_text or "hospitable" in merchant_text)
        ):
            vendor = "PriceLabs" if "pricelabs" in merchant_text else "Hospitable"
            software_sources[(vendor, owner)] += -amount

        if "shed rent" in " ".join((row.get("Merchant", ""), row.get("Notes", ""))).lower():
            shed_rows.append(row)

        note = row.get("Notes", "")
        is_pm_accrual = (
            prop in SCOPED_PROPERTIES
            and not account
            and amount < 0
            and ("AOPS-PNL-ACCRUAL|pm|" in note or "AOPS-PM-FEE|" in note)
        )
        if is_pm_accrual:
            dao = property_owner(prop)
            value = -amount
            pm_fee_accrued[dao] += value
            obligations[(dao, "ECO Systems LLC")] += value
            obligation_evidence.append({
                "date": row.get("ISODate", ""),
                "baselane_id": row.get("BaselaneId", ""),
                "source_account_owner": "Accounting-only PM fee accrual",
                "property_owner": dao,
                "property": prop,
                "amount": float(amount),
                "debtor": dao,
                "creditor": "ECO Systems LLC",
                "obligation_effect": float(value),
                "category": category,
                "merchant": row.get("Merchant", ""),
                "notes": note,
            })

        pm_text = " ".join((row.get("Merchant", ""), row.get("Description", ""), note)).lower()
        is_current_year_pm_payment = (
            owner in CORE_DAOS
            and amount < 0
            and "eco systems" in pm_text
            and "pm fee" in pm_text
            and re.search(
                r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
                r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+2026\b",
                pm_text,
            )
        )
        if is_current_year_pm_payment:
            value = -amount
            pm_fee_paid[owner] += value
            obligations[("ECO Systems LLC", owner)] += value
            obligation_evidence.append({
                "date": row.get("ISODate", ""),
                "baselane_id": row.get("BaselaneId", ""),
                "source_account_owner": owner,
                "property_owner": "ECO Systems LLC PM fee receivable",
                "property": prop,
                "amount": float(amount),
                "debtor": "ECO Systems LLC",
                "creditor": owner,
                "obligation_effect": float(value),
                "category": category,
                "merchant": row.get("Merchant", ""),
                "notes": note,
            })

        if not account or not prop:
            continue
        economic_owner = property_owner(prop)
        if economic_owner == owner:
            continue
        if owner not in CORE_DAOS and economic_owner not in CORE_DAOS:
            continue
        if amount < 0:
            debtor, creditor, value = economic_owner, owner, -amount
        elif amount > 0:
            debtor, creditor, value = owner, economic_owner, amount
        else:
            continue
        obligations[(debtor, creditor)] += value
        obligation_evidence.append({
            "date": row.get("ISODate", ""),
            "baselane_id": row.get("BaselaneId", ""),
            "source_account_owner": owner,
            "property_owner": economic_owner,
            "property": prop,
            "amount": float(amount),
            "debtor": debtor,
            "creditor": creditor,
            "obligation_effect": float(value),
            "category": category,
            "merchant": row.get("Merchant", ""),
            "notes": row.get("Notes", ""),
        })

    special_adjustments: list[dict[str, object]] = []

    source_ids = {str(row.get("BaselaneId") or "") for row in rows}
    eco_allocation_rows: list[dict[str, object]] = []
    if args.eco_allocation_report.is_file():
        eco_report = json.loads(args.eco_allocation_report.read_text(encoding="utf-8"))
        eco_allocated_cash_net = {
            str(item.get("bank_owner") or ""): money(item.get("net_eco_allocated_cash", "0"))
            for item in eco_report.get("accounts") or []
        }
        for row in eco_report.get("transactions") or []:
            baselane_id = str(row.get("baselane_id") or "")
            owner = str(row.get("bank_owner") or "")
            amount = money(row.get("amount", "0"))
            if owner not in CORE_DAOS or amount == 0:
                continue
            if str(row.get("tag_id") or "") in {"11", "20"}:
                no_dao_mortgage_pi_cash[owner] -= amount
            if baselane_id in source_ids:
                continue
            if amount < 0:
                debtor, creditor, value = "ECO Systems LLC", owner, -amount
            else:
                debtor, creditor, value = owner, "ECO Systems LLC", amount
            obligations[(debtor, creditor)] += value
            evidence = {
                "date": row.get("date", ""),
                "baselane_id": baselane_id,
                "source_account_owner": owner,
                "property_owner": "ECO Systems LLC",
                "property": "ECO Systems LLC",
                "amount": float(amount),
                "debtor": debtor,
                "creditor": creditor,
                "obligation_effect": float(value),
                "category": f"Baselane tagId {row.get('tag_id', '')}",
                "merchant": row.get("merchant", ""),
                "notes": row.get("notes", ""),
            }
            obligation_evidence.append(evidence)
            eco_allocation_rows.append(evidence)

    unpaid_shed_rows = [
        row for row in shed_rows
        if not row.get("Account")
        and row.get("Property") == "90 Madison Ave"
        and money(row.get("Amount", "0")) == Decimal("-50.00")
    ]
    if unpaid_shed_rows:
        debtor = PROPERTY_OWNERS["90 Madison Ave"]
        creditor = PROPERTY_OWNERS["86 Madison Ave"]
        unpaid_shed_total = Decimal("50.00") * len(unpaid_shed_rows)
        obligations[(debtor, creditor)] += unpaid_shed_total
        special_adjustments.append({
            "debtor": debtor,
            "creditor": creditor,
            "amount": float(unpaid_shed_total),
            "reason": "Accountless March-April shed-rent accruals that have not moved through the bank accounts.",
            "baselane_id": ",".join(row.get("BaselaneId", "") for row in unpaid_shed_rows),
        })

    transfers = net_pairwise(obligations)
    capital_by_entity: dict[str, dict[str, object]] = {}
    if args.capital_model.is_file():
        capital_report = json.loads(args.capital_model.read_text(encoding="utf-8"))
        for prop, entity in PROPERTY_OWNERS.items():
            item = (capital_report.get("properties") or {}).get(prop)
            if item:
                capital_by_entity[entity] = item.get("capital_waterfall") or {}
    for transfer in transfers:
        debtor = str(transfer["debtor"])
        creditor = str(transfer["creditor"])
        capital = capital_by_entity.get(debtor, {})
        debt = money(capital.get("total_debt", "0")) if capital else Decimal(0)
        unpaid_pm = max(Decimal(0), pm_fee_accrued.get(debtor, Decimal(0)) - pm_fee_paid.get(debtor, Decimal(0)))
        if creditor == "ECO Systems LLC" and unpaid_pm > 0:
            transfer["recommended_action"] = "Cash transfer; includes unpaid 2026 PM fee accruals"
        elif creditor == "ECO Systems LLC" and debt > 0:
            transfer["recommended_action"] = "Governance conversion to ECO equity/loan, or cash reimbursement if funded"
        elif debtor.startswith("Earl Co ") or creditor.startswith("Earl Co "):
            transfer["recommended_action"] = "Verify business purpose, then reimburse Earl Co or the DAO as shown"
        else:
            transfer["recommended_action"] = "Cash reimbursement"
    shed_by_month: dict[str, Counter] = defaultdict(Counter)
    for row in shed_rows:
        month = row["ISODate"][:7]
        kind = "actual" if row.get("Account") else "accounting_only"
        key = f"{row.get('Property')}|{row.get('Amount')}|{kind}|{row.get('Category')}"
        shed_by_month[month][key] += 1

    report = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "cutoff": args.cutoff.isoformat(),
        "source": str(args.source),
        "actual_scoped_dao_outflows": actual_scoped_outflows,
        "actual_scoped_dao_outflow_total": float(actual_scoped_outflow_total.quantize(MONEY)),
        "missing_property_or_category_tag_count": len(missing_tags),
        "remaining_non_property_expense_count": len(suspicious_non_property),
        "non_cash_close_row_count_excluded_from_settlement": len(non_cash_close_rows),
        "non_cash_close_amount_excluded_from_settlement": float(
            sum((money(row.get("Amount", "0")) for row in non_cash_close_rows), Decimal(0)).quantize(MONEY)
        ),
        "eco_direct_outflows_by_property": {key: float(value.quantize(MONEY)) for key, value in sorted(eco_paid.items())},
        "software_funding_sources": [
            {"vendor": vendor, "source_entity": owner, "net_outflow": float(value.quantize(MONEY))}
            for (vendor, owner), value in sorted(software_sources.items())
        ],
        "pm_fee_accruals_by_entity": {
            entity: float(value.quantize(MONEY)) for entity, value in sorted(pm_fee_accrued.items())
        },
        "pm_fee_cash_payments_by_entity": {
            entity: float(value.quantize(MONEY)) for entity, value in sorted(pm_fee_paid.items())
        },
        "unpaid_pm_fee_cash_by_entity": {
            entity: float(max(Decimal(0), pm_fee_accrued.get(entity, Decimal(0)) - pm_fee_paid.get(entity, Decimal(0))).quantize(MONEY))
            for entity in sorted(pm_fee_accrued)
        },
        "no_dao_mortgage_principal_interest_cash_by_entity": {
            entity: float(value.quantize(MONEY)) for entity, value in sorted(no_dao_mortgage_pi_cash.items())
        },
        "net_eco_allocated_cash_in_dao_bank_by_entity": {
            entity: float(value.quantize(MONEY)) for entity, value in sorted(eco_allocated_cash_net.items())
        },
        "cash_netting_policy": (
            "No-DAO-mortgage principal and interest are ECO cash obligations to the DAO bank accounts. "
            "The manual transfer schedule pairwise-nets those obligations against unpaid PM fees and other "
            "DAO-to-ECO reimbursements; the component totals remain separately disclosed."
        ),
        "shed_rent_by_month": {month: dict(counter) for month, counter in sorted(shed_by_month.items())},
        "special_adjustments": special_adjustments,
        "eco_allocation_report": str(args.eco_allocation_report) if args.eco_allocation_report.is_file() else None,
        "eco_allocation_transaction_count": len(eco_allocation_rows),
        "manual_transfer_schedule": transfers,
        "manual_transfer_total": float(sum((Decimal(str(row["amount"])) for row in transfers), Decimal(0)).quantize(MONEY)),
        "method": "Net actual 2026 bank activity by source-account owner versus economic property owner, including live ECO Systems LLC allocations held in DAO bank accounts. Accountless PM fee accruals payable to ECO are included; other accounting-only entries are excluded. Opposing reimbursements are netted only within the same legal entity pair.",
    }

    json_path = args.report_stem.with_suffix(".json")
    md_path = args.report_stem.with_suffix(".md")
    transfers_path = args.report_stem.with_name("scoped_dao_manual_transfer_schedule_20260714").with_suffix(".csv")
    evidence_path = args.report_stem.with_name("scoped_dao_interentity_cash_evidence_20260714").with_suffix(".csv")
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_csv(transfers_path, transfers, ["scope", "debtor", "creditor", "amount", "recommended_action"])
    write_csv(evidence_path, obligation_evidence, [
        "date", "baselane_id", "source_account_owner", "property_owner", "property", "amount",
        "debtor", "creditor", "obligation_effect", "category", "merchant", "notes",
    ])

    md: list[str] = [
        "# Scoped DAO Source-Funding Audit",
        "",
        f"Cutoff: {args.cutoff.isoformat()}",
        "",
        f"- Actual scoped DAO outflows checked: {actual_scoped_outflows} (${actual_scoped_outflow_total:,.2f})",
        f"- Missing property/category tags: {len(missing_tags)}",
        f"- Remaining `Non-Property Expense` outflows: {len(suspicious_non_property)}",
        "- PriceLabs and Hospitable are funded from the Heron/88 Madison DAO account, not ECO Systems checking.",
        f"- Live ECO-allocation rows added: {len(eco_allocation_rows)} (propertyId 37648; no duplicate Baselane IDs).",
        "- Chase checking ...0000 is Earl Co's personal account and is shown as an external reimbursement party.",
        "- Accountless PM fee accruals are payable to ECO and included in the cash-transfer schedule; other accounting-only close entries are excluded.",
        "",
        "## ECO Direct Funding",
        "",
    ]
    md.extend(f"- {prop}: ${value:,.2f}" for prop, value in sorted(eco_paid.items()))
    md.extend(["", "## PM Fee Cash Payables", ""])
    for entity in sorted(pm_fee_accrued):
        accrued = pm_fee_accrued[entity]
        paid = pm_fee_paid.get(entity, Decimal(0))
        md.append(
            f"- {entity}: accrued ${accrued:,.2f}; paid ${paid:,.2f}; cash due ${max(Decimal(0), accrued - paid):,.2f}"
        )
    md.extend(["", "## No-DAO-Mortgage Principal And Interest", ""])
    for entity in sorted(no_dao_mortgage_pi_cash):
        pi_cash = no_dao_mortgage_pi_cash[entity]
        eco_net = eco_allocated_cash_net.get(entity, Decimal(0))
        md.append(
            f"- {entity}: 2026 P&I cash paid by DAO bank ${pi_cash:,.2f}; "
            f"net ECO-allocated cash in that DAO bank ${eco_net:,.2f}"
        )
    md.extend([
        "",
        "P&I is a cash reimbursement from ECO to the DAO. The manual schedule below nets it against the same pair's PM fees and other reimbursements.",
    ])
    md.extend(["", "## Manual Transfers", ""])
    md.extend(
        f"- {row['debtor']} -> {row['creditor']}: ${Decimal(str(row['amount'])):,.2f} ({row['recommended_action']})"
        for row in transfers
    )
    md.extend(["", "## Shed Rent", ""])
    for month, counter in sorted(shed_by_month.items()):
        md.append(f"- {month}: " + "; ".join(f"{key} x{count}" for key, count in sorted(counter.items())))
    md.extend(["", "## Method", "", report["method"]])
    md_path.write_text("\n".join(md) + "\n", encoding="utf-8")

    print(json.dumps({
        "report": str(json_path),
        "markdown": str(md_path),
        "transfers": str(transfers_path),
        "evidence": str(evidence_path),
        "actual_outflows": actual_scoped_outflows,
        "missing_tags": len(missing_tags),
        "remaining_non_property": len(suspicious_non_property),
        "transfer_count": len(transfers),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
