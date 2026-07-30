#!/usr/bin/env python3
"""Build an authoritative live DAO cash reconciliation without moving money.

The report joins:

* live Baselane internal-transfer account balances;
* current property GL Column E totals;
* live savings/security-account transaction histories;
* the $500 active-property operating float; and
* explicitly documented security-deposit principal.

It is deliberately read-only.  Transfer execution requires a separate guarded
plan after every candidate has been reviewed for near-term mortgage and other
known cash obligations.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "baselane-mcp" / "src"))

from baselane_mcp.transfers import (  # noqa: E402
    list_active_transfer_accounts,
    run_graphql_via_cdp,
)


MONEY = Decimal("0.01")
DEFAULT_LEDGER = Path(
    "/mnt/c/Users/digit/Dropbox/Projects/assetrail/ECO Systems General Ledger.csv"
)
DEFAULT_REPORT = ROOT / "reports" / "baselane_live_dao_cash_reconciliation.json"
DEFAULT_CSV = ROOT / "reports" / "baselane_live_dao_cash_reconciliation.csv"
ACCOUNT_CLASSIFICATION_OVERRIDES = (
    ROOT / "config" / "baselane_bank_account_classification_overrides.json"
)
ACTIVE_SOURCE = ROOT / "reports" / "_goal_transfer_requirements.preview.json"
BRIDGE = ROOT / "scripts" / "baselane_graphql_via_cdp.js"
MANAGED_INTEREST_MARKER = "ECO bank interest through"
NONCASH_WEB3_MARKER = "WEB3-WEB2-RECON|"

# Baselane account nicknames and legacy source rows use short property names,
# while monthly accrual counterpart rows may use the full postal form.  Cash
# reconciliation must aggregate both forms into one property balance.
GL_PROPERTY_ALIASES = {
    "22164 Umland Cir, Jenner, CA 95450": "22164 Umland Circle",
    "326-332 S Alcott St": "326 South Alcott Street",
    "326-332 S Alcott St, Denver, CO 80219": "326 South Alcott Street",
    "49 Bannbury Ln, Palm Coast, FL 32137": "49 Bannbury Ln",
    "25 Circle Dr, Dixmoor, IL 60426": "25 Circle Dr",
    "5541 S Peoria St, Chicago, IL 60621": "5541 S Peoria St",
    "8143 S Sangamon St, Chicago, IL 60620": "8143 S Sangamon St.",
    "9 Country Club Lane N": "9 Country Club Ln N",
    "1278 E 187th St, Cleveland, OH 44110": "1278 E 187th St",
    "1432 Sara Ave, Akron, Ohio 44305": "1432 Sara Ave.",
    "428 Cross St, Akron, OH 44311": "428 Cross St.",
    "566 Nash St, Akron, OH 44306": "566 Nash St",
}


# Exact account nickname -> canonical property name used in the local GL.
# Non-property organizations (EARLDAO, NARWALL, ECO, EVCO, Earl Vanze Co) are
# intentionally outside this property-DAO cash reconciliation.
ACCOUNT_PROPERTY = {
    "10724 Gooding Ave Operations": "10724 Gooding Ave",
    "1278 E 187th St Operations": "1278 E 187th St",
    "1278 Security Deposits": "1278 E 187th St",
    "1315 E 114th St Operations": "1315 E 114th St",
    "1321 Allendale Ave Operations": "1321 Allendale Ave",
    "1432 Sara Ave Operations": "1432 Sara Ave.",
    "1456 W 85th St Operations": "1456 W 85th St.",
    "1518 Dille Rd Operations": "1518 Dille Rd",
    "15555 Millard Ave Operations": "15555 Millard Ave",
    "22164 Umland Circle Operations": "22164 Umland Circle",
    "22164 Umland Circle Reserves": "22164 Umland Circle",
    "25 Circle Dr Operations": "25 Circle Dr",
    "25 Circle Dr Security Deposits": "25 Circle Dr",
    "254 Bowmanville Operations": "254 Bowmanville St",
    "254 Bowmanville Reserves": "254 Bowmanville St",
    "27 Pillar Ln Operations": "27 Pillar Ln",
    "326-332 S Alcott Operations": "326 South Alcott Street",
    "326-332 S Alcott Reserves": "326 South Alcott Street",
    "326-332 Security Deposits": "326 South Alcott Street",
    "428 Cross St Operations": "428 Cross St.",
    "428 Security Deposits": "428 Cross St.",
    "49 Bannbury Ln Operations": "49 Bannbury Ln",
    "5401 Odom Ave Operations": "5401 Odom Ave",
    "5541 S Peoria St Operations": "5541 S Peoria St",
    "5541 S Peoria St Reserves": "5541 S Peoria St",
    "566 Nash St Operations": "566 Nash St",
    "566 Security Deposits": "566 Nash St",
    "724 3rd Ave Operations": "724 3rd Ave",
    "724 Reserves": "724 3rd Ave",
    "724 Security Deposits": "724 3rd Ave",
    "7542 & 7656 S Colfax Ave Reserves": "7542 & 7656 S Colfax Ave",
    "Lofty Holding 7542 & 7656 S Colfax Ave Operations": "7542 & 7656 S Colfax Ave",
    "804 S Quitman St Operations": "804 S Quitman St",
    "804 S Quitman St Reserves": "804 S Quitman St",
    "8143 S Sangamon St Operations": "8143 S Sangamon St.",
    "8143 S Sangamon St Reserves": "8143 S Sangamon St.",
    "84 Madison Ave Operations": "84 Madison Ave",
    "84 Madison Ave Reserves": "84 Madison Ave",
    "85-104 Alawa Pl Operations": "85-104 Alawa Pl",
    "86 Madison Ave Operations": "86 Madison Ave",
    "86 Madison Ave Reserves": "86 Madison Ave",
    "88 Madison Ave Operations": "88 Madison Ave",
    "88 Madison Ave Reserves": "88 Madison Ave",
    "9 Country Club Ln Operations": "9 Country Club Ln N",
    "9 Country Club Ln Reserves": "9 Country Club Ln N",
    "90 Madison Ave Operations": "90 Madison Ave",
    "90 Madison Ave Reserves": "90 Madison Ave",
    "918 Frederick Blvd Operations": "918 Frederick Blvd",
    "918 Security Deposits": "918 Frederick Blvd",
    "9634 S Green St Operations": "9634 S Green St",
    "9902 Garfield Ave Operations": "9902 Garfield Ave",
    "9902 Garfield Ave Reserves": "9902 Garfield Ave",
    "9919 S Oglesby Ave Deposits": "9919 S Oglesby Ave",
    "9919 S Oglesby Ave Operations": "9919 S Oglesby Ave",
    "9919 S Oglesby Ave Reserves": "9919 S Oglesby Ave",
    "Ohio-3 Security Deposits": "1518 Dille Rd",
    # The newer legal-entity accounts use generic nicknames.
    "Operating Account": "27 Pillar Ln",
}

# Baselane property accounts mapped above are Lofty coownership/DAO accounts.
# The portfolio capital-control policy adopted 2026-07-28 requires $3,000 of
# operating liquidity per coownership.  Non-coownership property accounts, if
# added later, must be listed explicitly rather than silently receiving the
# lower $500 operating floor.
COOWNERSHIP_OPERATING_FLOAT = Decimal("3000.00")
NON_COOWNERSHIP_OPERATING_FLOAT = Decimal("500.00")
NON_COOWNERSHIP_PROPERTIES: set[str] = {
    "1278 E 187th St",
    "1321 Allendale Ave",
    "1432 Sara Ave.",
    "1456 W 85th St.",
    "1518 Dille Rd",
    "25 Circle Dr",
    "3178 W 41st St",
    "428 Cross St.",
    "566 Nash St",
    "8143 S Sangamon St.",
    "918 Frederick Blvd",
    "9634 S Green St",
}


# Principal is established from live deposit/transfer history, not inferred
# from the current balance.  Any balance above this amount is ECO-owned
# interest.  Zero means all remaining cash in the security account is interest.
SECURITY_PRINCIPAL = {
    "118801": {
        "amount": Decimal("2250.00"),
        "basis": "Three $750 tenant-deposit principal lots; prior $15.38 interest swept.",
    },
    "152893": {
        "amount": Decimal("0.00"),
        "basis": "The $1,992.88 retained deposit was consolidated out; account is now zero.",
    },
    "152895": {
        "amount": Decimal("0.00"),
        "basis": "The $1,300 Ohio-3 deposit principal was fully distributed on 2025-12-22.",
    },
    "152896": {
        "amount": Decimal("0.00"),
        "basis": "The $1,545 principal and then-accrued $0.34 were fully removed in Dec 2025.",
    },
    "152897": {
        "amount": Decimal("1890.00"),
        "basis": "Current tenant-deposit principal was restored on 2026-05-16.",
    },
    "152898": {
        "amount": Decimal("749.00"),
        "basis": "Single $749 security-deposit wire received 2025-11-28.",
    },
    "157260": {
        "amount": Decimal("1050.00"),
        "basis": "$1,050 deposit transferred 2025-12-14; temporary $500 float was returned.",
    },
    "157734": {
        "amount": Decimal("0.00"),
        "basis": "No security-deposit activity and zero balance.",
    },
}

# Reserve-account principal still present after tracing every deposit,
# interest credit, and withdrawal in date order.  This is deliberately
# separate from security-deposit principal: it is not legally restricted, but
# it prevents a prior same-DAO movement of interest into operations from being
# mistaken for interest that is still physically in the reserve account.
RESERVE_RETAINED_PRINCIPAL = {
    "125710": {
        "amount": Decimal("0.00"),
        "basis": "The $0.31 reserve balance is the latest interest credit; $4.23 of earlier unswept interest moved to DAO operations.",
    },
    "129025": {
        "amount": Decimal("14.39"),
        "basis": "Sequence roll-forward leaves $14.39 of non-interest cash and $15.87 of unsettled bank interest.",
    },
    "126691": {
        "amount": Decimal("0.00"),
        "basis": "The 2026-06-23 $4,504.49 same-DAO withdrawal moved $4.49 of interest to operations; the remaining $7.05 is July-posted interest.",
    },
    "127684": {
        "amount": Decimal("4000.00"),
        "basis": "Sequence roll-forward leaves $4,000 reserve principal, $80.42 interest in reserves, and $3.89 previously moved to operations.",
    },
    "98200": {
        "amount": Decimal("0.00"),
        "basis": "Interest-inclusive same-DAO withdrawals moved $11.62 to operations; the remaining $3.18 is interest.",
    },
    "129026": {
        "amount": Decimal("0.00"),
        "basis": "The 2025-09-03 $2,006.22 same-DAO withdrawal moved $6.22 of interest to operations; $0.36 remains.",
    },
    "165515": {
        "amount": Decimal("5294.21"),
        "basis": "The 2026-07-15 $15,112.78 same-DAO withdrawal moved all then-held $112.78 interest to operations; subsequent ECO cash reconciliation settled $103.83, leaving $45.99 outside reserves.",
    },
    "90520": {
        "amount": Decimal("0.00"),
        "basis": "Interest-inclusive same-DAO withdrawals moved $12.93 to operations; $1.78 remains.",
    },
    "77898": {
        "amount": Decimal("0.00"),
        "basis": "Interest-inclusive same-DAO withdrawals moved $223.81 to operations; $0.20 remains.",
    },
    "150081": {
        "amount": Decimal("0.00"),
        "basis": "The current $25.34 balance consists entirely of bank interest posted after the reserve principal was emptied.",
    },
    "116479": {
        "amount": Decimal("0.00"),
        "basis": "The current $7.10 balance consists entirely of post-sweep bank interest; $28.53 of older unsettled interest is unfunded.",
    },
}


# Only transactions proven to settle Baselane bank-account interest belong
# here.  Keyword matching alone is unsafe: for example, 9902's $2 payment
# labeled "Accrued Interest ... Universal Lending DAO" predates the bank
# interest credits and is a loan expense, not a savings-interest sweep.
RESERVE_INTEREST_SETTLEMENTS = {
    "129025": {
        "207882737": {
            "amount": Decimal("2.83"),
            "basis": "Labeled ULD interest reconciliation and supported by the reserve roll-forward.",
        },
    },
    "126691": {
        "252181673": {
            "amount": Decimal("46.70"),
            "basis": "Exact cumulative bank interest through 2026-02, transferred to ECO after $37.83 of migrated interest was returned to reserves.",
        },
    },
    "127684": {
        "216028318": {
            "amount": Decimal("13.45"),
            "basis": "Labeled Sweep Interest to PM Account; exact cumulative bank interest through 2025-10.",
        },
    },
    "165515": {
        "240295694": {
            "amount": Decimal("1.04"),
            "basis": "Exact January 2026 interest credit transferred to ECO.",
        },
        "320815627": {
            "amount": Decimal("103.83"),
            "basis": "ECO cash reconciliation equal to the April and May 2026 interest credits ($37.04 + $66.79).",
        },
    },
    "116479": {
        "216336877": {
            "amount": Decimal("3.33"),
            "basis": "Labeled Interest Sweep to PM Account; exact November 2025 interest credit.",
        },
    },
}


TRANSACTION_QUERY = """
query Transactions($input: SortsAndFilters) {
  transactions(input: $input) {
    total
    data {
      id amount date merchantName description propertyId tagId bankAccountId
      note isSplit parentId isDeleted
    }
  }
}
"""


def money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(MONEY)


def note_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or "")
    return str(value or "")


def graphql(payload: dict[str, Any]) -> dict[str, Any]:
    return run_graphql_via_cdp(
        payload,
        bridge_path=BRIDGE,
        workspace_root=ROOT,
        timeout=120,
    )


def query_account_transactions(bank_account_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        result = graphql(
            {
                "operationName": "Transactions",
                "variables": {
                    "input": {
                        "sort": {"field": "date", "direction": "ASC"},
                        "filter": {
                            "isHidden": False,
                            "search": "",
                            "isCategorized": None,
                            "tagId": None,
                            "bankAccountId": bank_account_id,
                            "propertyId": None,
                            "unitId": None,
                            "isDeleted": False,
                            "isDocumentUploaded": None,
                        },
                        "page": page,
                        "pageLimit": 250,
                    }
                },
                "query": TRANSACTION_QUERY,
            }
        )["data"]["transactions"]
        batch = result.get("data") or []
        rows.extend(batch)
        if not batch or len(rows) >= int(result.get("total") or 0):
            return rows
        page += 1


def query_managed_interest_settlements() -> dict[str, list[dict[str, Any]]]:
    """Return exact tool-created interest debit mirrors by source bank account.

    The controlled bookkeeping-note prefix is deliberately stronger evidence
    than a generic "interest" keyword.  Only negative, unsplit transfer
    parents tagged Transfers Between Accounts are accepted.
    """
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        result = graphql(
            {
                "operationName": "Transactions",
                "variables": {
                    "input": {
                        "sort": {"field": "date", "direction": "ASC"},
                        "filter": {
                            "isHidden": False,
                            "search": "",
                            "isCategorized": None,
                            "tagId": "24",
                            "bankAccountId": None,
                            "propertyId": None,
                            "unitId": None,
                            "isDeleted": False,
                            "isDocumentUploaded": None,
                        },
                        "page": page,
                        "pageLimit": 250,
                    }
                },
                "query": TRANSACTION_QUERY,
            }
        )["data"]["transactions"]
        batch = result.get("data") or []
        rows.extend(batch)
        if not batch or len(rows) >= int(result.get("total") or 0):
            break
        page += 1

    matched: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        note = " ".join(note_text(row.get("note")).split())
        if (
            money(row.get("amount")) >= 0
            or row.get("parentId")
            or row.get("isDeleted")
            or str(row.get("tagId") or "") != "24"
            or not note.startswith(MANAGED_INTEREST_MARKER)
        ):
            continue
        matched[str(row.get("bankAccountId") or "")].append(
            {
                "transaction_id": str(row.get("id") or ""),
                "date": str(row.get("date") or ""),
                "amount": f"{abs(money(row.get('amount'))):.2f}",
                "bookkeeping_note": note,
                "property_id": str(row.get("propertyId") or ""),
            }
        )
    return matched


def active_gl_names() -> set[str]:
    if not ACTIVE_SOURCE.exists():
        return set()
    data = json.loads(ACTIVE_SOURCE.read_text(encoding="utf-8"))
    return {
        GL_PROPERTY_ALIASES.get(str(row["property"]), str(row["property"]))
        for row in data.get("active_dao_cash_balance_rows", [])
    }


def read_gl(path: Path, cutoff: date) -> tuple[dict[str, Decimal], dict[str, Decimal], dict[str, int]]:
    full: dict[str, Decimal] = defaultdict(Decimal)
    cash_basis: dict[str, Decimal] = defaultdict(Decimal)
    counts: dict[str, int] = defaultdict(int)
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            row_date = datetime.strptime(row["Date"], "%B %d, %Y").date()
            if row_date > cutoff:
                continue
            raw_prop = str(row.get("Property") or "").strip()
            prop = GL_PROPERTY_ALIASES.get(raw_prop, raw_prop)
            if not prop:
                continue
            amount = money(row.get("Amount"))
            full[prop] += amount
            counts[prop] += 1
            # Retained-capital P&L overlays are accounting-only.  For EARLDAO
            # Web2/Web3 settlement, an incoming share leg is also noncash and
            # cannot increase the DAO's bank-cash target.  A negative property
            # leg does remain in the cash basis because it clears cash that the
            # DAO otherwise owed to Yhome.
            notes = str(row.get("Notes") or "")
            if (
                "AOPS-PNL-ACCRUAL" not in notes
                and not (
                    NONCASH_WEB3_MARKER in notes
                    and amount > 0
                )
            ):
                cash_basis[prop] += amount
    return full, cash_basis, counts


def savings_evidence(account: dict[str, Any]) -> dict[str, Any]:
    balance = money(account["available_balance"])
    rows = query_account_transactions(str(account["bank_account_id"]))
    credits = [
        row
        for row in rows
        if str(row.get("merchantName") or "").strip().lower().startswith("interest ")
        and money(row.get("amount")) > 0
    ]
    keyword_interest_debits = [
        row
        for row in rows
        if money(row.get("amount")) < 0
        and "interest" in (
            f"{row.get('merchantName') or ''} "
            f"{row.get('description') or ''} {note_text(row.get('note'))}"
        ).lower()
    ]
    gross = sum((money(row.get("amount")) for row in credits), Decimal())
    bank_id = str(account["bank_account_id"])
    row_by_id = {str(row.get("id") or ""): row for row in rows}
    settlement_specs = RESERVE_INTEREST_SETTLEMENTS.get(bank_id, {})
    verified_settlements: list[dict[str, Any]] = []
    settlement_issue = None
    for transaction_id, spec in settlement_specs.items():
        row = row_by_id.get(transaction_id)
        if row is None:
            settlement_issue = f"documented interest settlement {transaction_id} is missing"
            continue
        actual = abs(money(row.get("amount")))
        expected = money(spec["amount"])
        if money(row.get("amount")) >= 0 or actual != expected:
            settlement_issue = (
                f"documented interest settlement {transaction_id} expected "
                f"-{expected:.2f}, found {money(row.get('amount')):.2f}"
            )
            continue
        verified_settlements.append(
            {
                "transaction_id": transaction_id,
                "amount": f"{actual:.2f}",
                "basis": spec["basis"],
            }
        )
    settled = sum(
        (money(row["amount"]) for row in verified_settlements),
        Decimal(),
    )
    economic_unsettled = max(Decimal(), gross - settled)
    principal_meta = SECURITY_PRINCIPAL.get(bank_id)
    if principal_meta is not None:
        principal = principal_meta["amount"]
        interest_held = balance - principal
        status = "exact_security_principal"
        issue = settlement_issue
        if interest_held < 0:
            issue = (
                f"security balance {balance:.2f} is below documented principal "
                f"{principal:.2f}"
            )
        reserve_principal = None
        reserve_principal_basis = None
        interest_cash_in_savings = interest_held
    else:
        principal = None
        reserve_meta = RESERVE_RETAINED_PRINCIPAL.get(bank_id)
        reserve_principal = (
            money(reserve_meta["amount"]) if reserve_meta is not None else None
        )
        reserve_principal_basis = (
            reserve_meta["basis"] if reserve_meta is not None else None
        )
        if reserve_principal is None:
            interest_cash_in_savings = min(balance, economic_unsettled)
            status = "candidate_requires_sequence_review"
            issue = settlement_issue
        else:
            interest_cash_in_savings = max(Decimal(), balance - reserve_principal)
            interest_cash_in_savings = min(
                interest_cash_in_savings,
                economic_unsettled,
            )
            status = "exact_sequence_rollforward"
            issue = settlement_issue
        # For reserve accounts this is the full economic amount still owed by
        # the DAO.  It may exceed the savings balance because prior same-DAO
        # withdrawals moved interest into operations.
        interest_held = economic_unsettled
    return {
        "transaction_count": len(rows),
        "interest_credit_count": len(credits),
        "gross_interest_credits": f"{gross:.2f}",
        "verified_interest_settlements": f"{settled:.2f}",
        "economic_unsettled_interest": f"{economic_unsettled:.2f}",
        "documented_security_principal": (
            f"{principal:.2f}" if principal is not None else None
        ),
        "security_principal_basis": (
            principal_meta["basis"] if principal_meta is not None else None
        ),
        "documented_reserve_principal": (
            f"{reserve_principal:.2f}" if reserve_principal is not None else None
        ),
        "reserve_principal_basis": reserve_principal_basis,
        "interest_cash_in_savings_account": f"{interest_cash_in_savings:.2f}",
        "interest_held": f"{interest_held:.2f}",
        "interest_status": status,
        "issue": issue,
        "interest_credit_ids": [str(row.get("id") or "") for row in credits],
        "verified_interest_settlement_rows": verified_settlements,
        "keyword_interest_debit_ids_not_used_as_proof": [
            str(row.get("id") or "") for row in keyword_interest_debits
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    args = parser.parse_args()

    live = list_active_transfer_accounts(graphql)
    account_overrides = json.loads(
        ACCOUNT_CLASSIFICATION_OVERRIDES.read_text(encoding="utf-8")
    )
    managed_interest_by_bank = query_managed_interest_settlements()
    unique_accounts: dict[str, dict[str, Any]] = {}
    unmapped_property_dao_accounts: list[dict[str, Any]] = []
    for account in live:
        bank_id = str(account["bank_account_id"])
        unique_accounts[bank_id] = account
    property_accounts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    excluded_accounts: list[dict[str, Any]] = []
    for account in unique_accounts.values():
        bank_id = str(account["bank_account_id"])
        override = account_overrides.get(bank_id)
        if override and override.get("exclude_from_dao_cash"):
            excluded_accounts.append(
                {
                    "bank_account_id": bank_id,
                    "transfer_account_id": int(account["transfer_account_id"]),
                    "account_name": account.get("account_name"),
                    "nickname": account.get("nickname"),
                    "available_balance": str(account.get("available_balance")),
                    **override,
                }
            )
            continue
        nickname = str(account.get("nickname") or "")
        prop = ACCOUNT_PROPERTY.get(nickname)
        owner = str(account.get("account_name") or "")
        looks_like_property_dao = "DAO LLC" in owner.upper() or owner in {
            "LFTY0412 LLC",
            "LFTY400 LLC",
        }
        if prop:
            enriched = dict(account)
            enriched["property"] = prop
            property_accounts[prop].append(enriched)
        elif looks_like_property_dao and owner != "Ecosystems Asset Recovery Lending DAO LLC":
            unmapped_property_dao_accounts.append(account)

    full_gl, cash_basis_gl, gl_counts = read_gl(args.ledger, args.as_of)
    active = active_gl_names()
    rows: list[dict[str, Any]] = []
    issues: list[str] = []
    for prop in sorted(property_accounts):
        accounts = sorted(
            property_accounts[prop],
            key=lambda row: (row["account_subtype"], row["nickname"]),
        )
        account_rows = []
        security_principal = Decimal()
        exact_interest = Decimal()
        reserve_interest_due = Decimal()
        reserve_interest_cash = Decimal()
        managed_reserve_interest_settled = Decimal()
        managed_interest_settlement_rows: list[dict[str, Any]] = []
        ops_balance = Decimal()
        total_balance = Decimal()
        for account in accounts:
            balance = money(account["available_balance"])
            total_balance += balance
            if account["account_subtype"] == "checking":
                ops_balance += balance
            evidence = None
            if (
                account["account_subtype"] == "savings"
                and (
                    balance != 0
                    or str(account["bank_account_id"]) in managed_interest_by_bank
                )
            ):
                last_error: Exception | None = None
                for attempt in range(3):
                    try:
                        evidence = savings_evidence(account)
                        last_error = None
                        break
                    except Exception as exc:
                        last_error = exc
                        if attempt < 2:
                            time.sleep(2)
                if last_error is not None:  # preserve partial audit and surface the gap
                    evidence = {
                        "status": "query_failed",
                        "error": str(last_error),
                        "documented_security_principal": None,
                        "interest_held": "0.00",
                    }
                    issues.append(
                        f"{prop} / {account['nickname']}: savings history query failed"
                    )
            elif account["account_subtype"] == "savings":
                evidence = {
                    "status": "zero_balance_no_query_needed",
                    "documented_security_principal": (
                        f"{SECURITY_PRINCIPAL[str(account['bank_account_id'])]['amount']:.2f}"
                        if str(account["bank_account_id"]) in SECURITY_PRINCIPAL
                        else None
                    ),
                    "interest_held": "0.00",
                    "issue": None,
                }
            if account["account_subtype"] == "savings":
                if evidence["documented_security_principal"] is not None:
                    security_principal += money(
                        evidence["documented_security_principal"]
                    )
                    exact_interest += money(evidence["interest_held"])
                    if evidence.get("issue"):
                        issues.append(
                            f"{prop} / {account['nickname']}: {evidence['issue']}"
                        )
                elif (
                    evidence.get("status") != "query_failed"
                    and "economic_unsettled_interest" in evidence
                ):
                    reserve_interest_due += money(
                        evidence["economic_unsettled_interest"]
                    )
                    reserve_interest_cash += money(
                        evidence["interest_cash_in_savings_account"]
                    )
            for settlement in managed_interest_by_bank.get(
                str(account["bank_account_id"]),
                [],
            ):
                self_reflecting_security = (
                    str(account["bank_account_id"]) in SECURITY_PRINCIPAL
                )
                managed_interest_settlement_rows.append(
                    {
                        **settlement,
                        "bank_account_id": str(account["bank_account_id"]),
                        "nickname": str(account["nickname"]),
                        "accounting": (
                            "current_security_balance_already_reflects_settlement"
                            if self_reflecting_security
                            else "subtract_from_reserve_interest_economic_due"
                        ),
                    }
                )
                if not self_reflecting_security:
                    managed_reserve_interest_settled += money(
                        settlement["amount"]
                    )
            account_rows.append(
                {
                    "bank_account_id": str(account["bank_account_id"]),
                    "transfer_account_id": int(account["transfer_account_id"]),
                    "account_name": account["account_name"],
                    "nickname": account["nickname"],
                    "subtype": account["account_subtype"],
                    "available_balance": f"{balance:.2f}",
                    "savings_evidence": evidence,
                }
            )

        is_active = prop in active
        if is_active:
            operating_floor = (
                NON_COOWNERSHIP_OPERATING_FLOAT
                if prop in NON_COOWNERSHIP_PROPERTIES
                else COOWNERSHIP_OPERATING_FLOAT
            )
        else:
            operating_floor = Decimal()
        protected_floor = operating_floor + security_principal
        gl_full = full_gl.get(prop, Decimal())
        gl_cash = cash_basis_gl.get(prop, Decimal())
        target = max(gl_cash, protected_floor)
        excess = max(Decimal(), total_balance - target)
        shortfall = max(Decimal(), target - total_balance)
        reserve_interest_due_before_managed = reserve_interest_due
        reserve_interest_due = max(
            Decimal(),
            reserve_interest_due - managed_reserve_interest_settled,
        )
        interest_due = exact_interest + reserve_interest_due
        interest_cash_capacity = max(
            Decimal(),
            total_balance - security_principal - operating_floor,
        )
        interest_transfer = min(interest_due, interest_cash_capacity)
        interest_unfunded = max(Decimal(), interest_due - interest_transfer)

        # Prefer the account in which the interest is presently identifiable,
        # then use operations for interest previously moved there by a
        # same-DAO transfer.  Security principal and the active-property float
        # are never sources.
        remaining_interest_transfer = interest_transfer
        interest_transfer_sources: list[dict[str, Any]] = []
        for account_row in account_rows:
            evidence = account_row.get("savings_evidence") or {}
            if account_row["subtype"] != "savings":
                continue
            source_cash = money(
                evidence.get("interest_cash_in_savings_account") or 0
            )
            amount = min(source_cash, remaining_interest_transfer)
            if amount <= 0:
                continue
            interest_transfer_sources.append(
                {
                    "transfer_account_id": account_row["transfer_account_id"],
                    "bank_account_id": account_row["bank_account_id"],
                    "nickname": account_row["nickname"],
                    "amount": f"{amount:.2f}",
                    "source": "savings_interest_cash",
                }
            )
            remaining_interest_transfer -= amount
        ops_floor_remaining = operating_floor
        for account_row in account_rows:
            if remaining_interest_transfer <= 0:
                break
            if account_row["subtype"] != "checking":
                continue
            available = money(account_row["available_balance"])
            protected_here = min(available, ops_floor_remaining)
            ops_floor_remaining -= protected_here
            available -= protected_here
            amount = min(available, remaining_interest_transfer)
            if amount <= 0:
                continue
            interest_transfer_sources.append(
                {
                    "transfer_account_id": account_row["transfer_account_id"],
                    "bank_account_id": account_row["bank_account_id"],
                    "nickname": account_row["nickname"],
                    "amount": f"{amount:.2f}",
                    "source": "interest_migrated_to_operations",
                }
            )
            remaining_interest_transfer -= amount
        if remaining_interest_transfer > 0:
            issues.append(
                f"{prop}: validated interest transfer {interest_transfer:.2f} "
                f"exceeds traced source capacity by {remaining_interest_transfer:.2f}"
            )
        # The arithmetic identifies a candidate, not authorization.  Near-term
        # mortgage/escrow obligations and already-staged net settlements must
        # be applied before a transfer plan is safe.
        recommendation = "candidate_requires_obligation_review"
        if excess == 0:
            recommendation = "no_excess_cash"
        rows.append(
            {
                "property": prop,
                "active": is_active,
                "gl_row_count_as_of": gl_counts.get(prop, 0),
                "gl_column_e_full_as_of": f"{gl_full:.2f}",
                "gl_cash_settlement_basis_as_of": f"{gl_cash:.2f}",
                "dao_bank_total": f"{total_balance:.2f}",
                "operations_balance": f"{ops_balance:.2f}",
                "documented_security_principal": f"{security_principal:.2f}",
                "exact_interest_held_in_security_accounts": f"{exact_interest:.2f}",
                "reserve_interest_economic_unsettled": f"{reserve_interest_due:.2f}",
                "reserve_interest_economic_before_managed_settlements": (
                    f"{reserve_interest_due_before_managed:.2f}"
                ),
                "managed_interest_settled_total": (
                    f"{managed_reserve_interest_settled:.2f}"
                ),
                "managed_interest_settlement_rows": (
                    managed_interest_settlement_rows
                ),
                "reserve_interest_cash_in_savings": f"{reserve_interest_cash:.2f}",
                "total_interest_due_to_eco": f"{interest_due:.2f}",
                "validated_interest_cash_transfer": f"{interest_transfer:.2f}",
                "unfunded_interest_accrual": f"{interest_unfunded:.2f}",
                "interest_transfer_sources": interest_transfer_sources,
                "operating_float": f"{operating_floor:.2f}",
                "protected_minimum": f"{protected_floor:.2f}",
                "cash_target_before_known_obligations": f"{target:.2f}",
                "candidate_excess_before_known_obligations": f"{excess:.2f}",
                "cash_shortfall": f"{shortfall:.2f}",
                "recommendation": recommendation,
                "accounts": account_rows,
            }
        )

    status = "ok" if not issues and not unmapped_property_dao_accounts else "review_required"
    report = {
        "status": status,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "as_of": args.as_of.isoformat(),
        "ledger": str(args.ledger),
        "policy": {
            "internal_transfers_only": True,
            "coownership_operations_float": f"{COOWNERSHIP_OPERATING_FLOAT:.2f}",
            "non_coownership_operations_float": f"{NON_COOWNERSHIP_OPERATING_FLOAT:.2f}",
            "non_coownership_properties": sorted(NON_COOWNERSHIP_PROPERTIES),
            "security_principal_is_restricted": True,
            "interest_belongs_to_eco": True,
            "aops_pnl_accrual_excluded_from_cash_basis": True,
            "candidate_excess_is_not_execution_authority": True,
            "known_obligations_must_be_reviewed_before_transfer": True,
            "account_classification_overrides": str(
                ACCOUNT_CLASSIFICATION_OVERRIDES
            ),
        },
        "property_count": len(rows),
        "issues": issues,
        "excluded_accounts": excluded_accounts,
        "unmapped_property_dao_accounts": unmapped_property_dao_accounts,
        "properties": rows,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    flat_columns = [
        "property",
        "active",
        "gl_column_e_full_as_of",
        "gl_cash_settlement_basis_as_of",
        "dao_bank_total",
        "operations_balance",
        "documented_security_principal",
        "exact_interest_held_in_security_accounts",
        "reserve_interest_economic_unsettled",
        "reserve_interest_cash_in_savings",
        "total_interest_due_to_eco",
        "validated_interest_cash_transfer",
        "unfunded_interest_accrual",
        "operating_float",
        "protected_minimum",
        "cash_target_before_known_obligations",
        "candidate_excess_before_known_obligations",
        "cash_shortfall",
        "recommendation",
    ]
    with args.csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=flat_columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row[key] for key in flat_columns})
    print(
        json.dumps(
            {
                "status": status,
                "as_of": args.as_of.isoformat(),
                "report": str(args.report),
                "csv": str(args.csv),
                "property_count": len(rows),
                "issue_count": len(issues),
                "unmapped_property_dao_account_count": len(
                    unmapped_property_dao_accounts
                ),
            },
            indent=2,
        )
    )
    return 0 if status == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
