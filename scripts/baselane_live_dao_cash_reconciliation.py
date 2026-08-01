#!/usr/bin/env python3
"""Build an authoritative live DAO cash reconciliation without moving money.

The report joins:

* live Baselane internal-transfer account balances;
* current property GL Column E totals;
* live savings/security-account transaction histories;
* any local-bank operating float that is separate from the portfolio reserve;
* explicitly documented security-deposit principal.

It is deliberately read-only.  Transfer execution requires a separate guarded
plan after every candidate has been reviewed for near-term mortgage and other
known cash obligations.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
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
from coownership_reserve_policy import (  # noqa: E402
    outstanding_manual_accrual_liability,
    row_date as policy_row_date,
    row_matches_property,
)
from coownership_mortgage_policy import is_no_dao_mortgage_property  # noqa: E402


MONEY = Decimal("0.01")
DEFAULT_LEDGER = Path(
    "/mnt/c/Users/digit/Dropbox/Projects/assetrail/ECO Systems General Ledger.csv"
)
DEFAULT_CUSTODY_LEDGER = ROOT / "reports" / "baselane_source_transaction_index.csv"
DEFAULT_REPORT = ROOT / "reports" / "baselane_live_dao_cash_reconciliation.json"
DEFAULT_CSV = ROOT / "reports" / "baselane_live_dao_cash_reconciliation.csv"
ACCOUNT_CLASSIFICATION_OVERRIDES = (
    ROOT / "config" / "baselane_bank_account_classification_overrides.json"
)
ACTIVE_SOURCE = ROOT / "reports" / "_goal_transfer_requirements.preview.json"
BRIDGE = ROOT / "scripts" / "baselane_graphql_via_cdp.js"
MANAGED_INTEREST_MARKER = "ECO bank interest through"
NONCASH_WEB3_MARKER = "WEB3-WEB2-RECON|"
ECO_ACCOUNT_PREFIX = "ECO Systems, LLC-"
ECO_EARNED_REVENUE_CATEGORIES = {
    "fees & other revenue",
    "management fees",
    "property management",
    "interest received",
    "interest income",
    "other interests",
}
NONCASH_ROW_MARKERS = (
    "aops-pnl-accrual|",
    "aops-monthly-accrual|",
    "web3-web2-recon|",
)

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
    "85-104 Alawa Pl Reserves": "85-104 Alawa Pl",
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
# combined co-ownership liquidity across positive Lofty Operating Reserve and
# ECO-held spendable DAO cash. It is not a second $3,000 minimum that must sit
# in a Baselane account. This read-only bank reconciliation therefore applies
# no separate co-ownership local-bank float; the downstream transfer planner
# enforces the combined floor using the current Lofty OR balance.
COOWNERSHIP_COMBINED_RESERVE_FLOOR = Decimal("3000.00")
COOWNERSHIP_LOCAL_BANK_FLOAT = Decimal("0.00")
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
            raw_date = str(row.get("Date") or "").strip()
            try:
                row_date = date.fromisoformat(raw_date)
            except ValueError:
                row_date = datetime.strptime(raw_date, "%B %d, %Y").date()
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


def normalized_text(*values: Any) -> str:
    return re.sub(r"\s+", " ", " ".join(str(value or "") for value in values)).strip().casefold()


def truthy(value: Any) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "y"}


def eco_intercompany_row_classification(row: dict[str, Any], prop: str) -> tuple[str, str]:
    """Classify an ECO-account property row for the DAO/ECO reciprocal subledger.

    Included cash is a transaction-backed intercompany position: positive
    amounts are DAO cash received by ECO or repayments; negative amounts are
    ECO cash used for the DAO.  ECO-earned revenue and ECO's own obligations
    are excluded so they cannot become either DAO custody or a DAO payable.
    """
    if normalized_text(prop) in {"earldao", "eco systems llc", "earl vanze co", "narwall", "evco"}:
        return "exclude", "non_dao_or_separate_counterparty"
    if truthy(row.get("Pending")):
        return "exclude", "pending_bank_transaction"
    if not str(row.get("BaselaneId") or "").strip():
        return "exclude", "missing_source_transaction_id"
    row_type = normalized_text(row.get("Type"))
    notes = normalized_text(row.get("Notes"))
    if "manual" in row_type or any(marker in notes for marker in NONCASH_ROW_MARKERS):
        return "exclude", "accounting_only_or_manual_row"
    amount = money(row.get("Amount"))
    if amount == 0:
        return "exclude", "zero_amount"
    category = normalized_text(row.get("Category"))
    subcategory = normalized_text(row.get("Sub-category"))
    text = normalized_text(
        row.get("Merchant"), row.get("Description"), row.get("Notes"), category, subcategory
    )
    if category in ECO_EARNED_REVENUE_CATEGORIES or subcategory in ECO_EARNED_REVENUE_CATEGORIES:
        return "exclude", "eco_earned_revenue"
    if amount > 0 and re.search(
        r"\b(pm|property management|management|dao|llc|registration) fee(s)?\b", text
    ):
        return "exclude", "eco_fee_cash_settlement"
    if "credit card payment" in category or "credit card payment" in subcategory:
        return "exclude", "credit_card_payoff_duplicate"
    if "ecosystems asset recovery lending dao" in text or "earldao" in text:
        return "exclude", "earldao_is_separate_counterparty"
    if "transfer" in category and re.search(r"\beco systems[, ]+(llc)?\b", text):
        return "exclude", "eco_to_eco_internal_transfer"
    if amount < 0 and is_no_dao_mortgage_property(prop):
        escrow_component = any(token in text for token in ("escrow", "property tax", "insurance"))
        eco_mortgage_obligation = any(
            token in text
            for token in (
                "mortgage principal",
                "principal payment",
                "mortgage interest",
                "interest payment",
                "late fee",
                "late charge",
                "nsf",
                "returned payment fee",
            )
        )
        if eco_mortgage_obligation and not escrow_component:
            return "exclude", "eco_no_dao_mortgage_obligation"
    return "include", "dao_cash_received_or_eco_cash_advanced"


def build_eco_intercompany_subledger(
    source_rows: list[dict[str, Any]], cutoff: date
) -> dict[str, dict[str, Any]]:
    positions: dict[str, dict[str, Any]] = {}
    for row in source_rows:
        if not str(row.get("Account") or "").startswith(ECO_ACCOUNT_PREFIX):
            continue
        row_date = policy_row_date(row)
        if row_date is None or row_date > cutoff:
            continue
        raw_prop = str(row.get("Property") or "").strip()
        prop = GL_PROPERTY_ALIASES.get(raw_prop, raw_prop)
        if not prop:
            continue
        item = positions.setdefault(
            prop,
            {
                "property": prop,
                "included_position": Decimal(),
                "gross_eco_advances": Decimal(),
                "gross_dao_cash_credits": Decimal(),
                "included_rows": [],
                "excluded_rows": [],
                "monthly": defaultdict(lambda: {"eco_advances": Decimal(), "dao_cash_credits": Decimal()}),
                "categories": defaultdict(lambda: {"eco_advances": Decimal(), "dao_cash_credits": Decimal()}),
            },
        )
        action, reason = eco_intercompany_row_classification(row, prop)
        amount = money(row.get("Amount"))
        evidence = {
            "date": row_date.isoformat(),
            "baselane_id": str(row.get("BaselaneId") or ""),
            "amount": f"{amount:.2f}",
            "category": str(row.get("Category") or ""),
            "subcategory": str(row.get("Sub-category") or ""),
            "merchant": str(row.get("Merchant") or ""),
            "notes": str(row.get("Notes") or ""),
            "classification": reason,
        }
        if action == "exclude":
            item["excluded_rows"].append(evidence)
            continue
        item["included_position"] += amount
        direction = "dao_cash_credits" if amount > 0 else "eco_advances"
        value = amount if amount > 0 else -amount
        if amount > 0:
            item["gross_dao_cash_credits"] += value
        else:
            item["gross_eco_advances"] += value
        item["monthly"][row_date.strftime("%Y-%m")][direction] += value
        category = str(row.get("Category") or "Uncategorized").strip() or "Uncategorized"
        item["categories"][category][direction] += value
        item["included_rows"].append(evidence)

    output: dict[str, dict[str, Any]] = {}
    for prop, item in positions.items():
        position = item["included_position"].quantize(MONEY)
        output[prop] = {
            "property": prop,
            "status": "ok",
            "source_mode": "id_bearing_eco_account_intercompany_subledger",
            "eco_intercompany_net_position": f"{position:.2f}",
            "eco_held_dao_cash_before_obligations": f"{max(Decimal(), position):.2f}",
            "dao_accounts_payable_to_eco": f"{max(Decimal(), -position):.2f}",
            "eco_accounts_receivable_from_dao": f"{max(Decimal(), -position):.2f}",
            "gross_eco_advances": f"{item['gross_eco_advances']:.2f}",
            "gross_dao_cash_credits": f"{item['gross_dao_cash_credits']:.2f}",
            "included_row_count": len(item["included_rows"]),
            "excluded_row_count": len(item["excluded_rows"]),
            "monthly_breakdown": [
                {
                    "month": month,
                    "eco_advances": f"{values['eco_advances']:.2f}",
                    "dao_cash_credits": f"{values['dao_cash_credits']:.2f}",
                    "net_change": f"{values['dao_cash_credits'] - values['eco_advances']:.2f}",
                }
                for month, values in sorted(item["monthly"].items())
            ],
            "category_breakdown": [
                {
                    "category": category,
                    "eco_advances": f"{values['eco_advances']:.2f}",
                    "dao_cash_credits": f"{values['dao_cash_credits']:.2f}",
                    "net_change": f"{values['dao_cash_credits'] - values['eco_advances']:.2f}",
                }
                for category, values in sorted(item["categories"].items())
            ],
            "included_rows": item["included_rows"],
            "excluded_rows": item["excluded_rows"],
        }
    return output


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
    parser.add_argument(
        "--custody-ledger",
        type=Path,
        default=DEFAULT_CUSTODY_LEDGER,
        help=(
            "ID-bearing Baselane transaction index with source bank-account ownership. "
            "The normalized property GL remains the accounting ledger."
        ),
    )
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    args = parser.parse_args()

    source_rows = list(
        csv.DictReader(args.custody_ledger.open(encoding="utf-8-sig", newline=""))
    )
    intercompany_subledger = build_eco_intercompany_subledger(source_rows, args.as_of)
    custody_source_ok = any(
        str(row.get("Account") or "").startswith(ECO_ACCOUNT_PREFIX)
        for row in source_rows
    )
    source_sha256 = hashlib.sha256(args.ledger.read_bytes()).hexdigest()
    custody_source_sha256 = hashlib.sha256(args.custody_ledger.read_bytes()).hexdigest()

    # A negative Yhome balance is an ECO-cash restriction for the property;
    # a positive balance due from Yhome is represented by EARLDAO shares and
    # is not cash in ECO's custody.  Prefer the newest audit generated from
    # this exact ID-bearing source index.
    yhome_restrictions: dict[str, Decimal] = {}
    for candidate in sorted(
        (ROOT / "reports").glob("yhome_all_property_eco_cash_audit*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    ):
        try:
            audit = json.loads(candidate.read_text(encoding="utf-8"))
            audit_source = Path(
                str((audit.get("sources") or {}).get("source_index") or "")
            ).resolve()
        except (OSError, json.JSONDecodeError):
            continue
        if audit_source != args.custody_ledger.resolve():
            continue
        for item in audit.get("properties") or []:
            source_property = str(item.get("source_property") or "").strip()
            adjustment = money(item.get("expected_negative_yhome_adjustment"))
            if source_property and adjustment < 0:
                yhome_restrictions[source_property] = -adjustment
        break

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
                else COOWNERSHIP_LOCAL_BANK_FLOAT
            )
        else:
            operating_floor = Decimal()
        protected_floor = operating_floor + security_principal
        gl_full = full_gl.get(prop, Decimal())
        gl_cash = cash_basis_gl.get(prop, Decimal())
        property_source_rows = [
            row
            for row in source_rows
            if row_matches_property(row, prop)
            and (policy_row_date(row) is not None)
            and policy_row_date(row) <= args.as_of
        ]
        intercompany = intercompany_subledger.get(prop) or {
            "status": "ok",
            "eco_intercompany_net_position": "0.00",
            "eco_held_dao_cash_before_obligations": "0.00",
            "dao_accounts_payable_to_eco": "0.00",
            "eco_accounts_receivable_from_dao": "0.00",
            "gross_eco_advances": "0.00",
            "gross_dao_cash_credits": "0.00",
            "included_row_count": 0,
            "excluded_row_count": 0,
            "monthly_breakdown": [],
            "category_breakdown": [],
        }
        eco_attributed_account_activity = money(intercompany["eco_intercompany_net_position"])
        # The signed, ID-bearing intercompany subledger is bifurcated rather
        # than presented as negative cash.  A positive position is DAO cash
        # held by ECO.  A negative position is the reciprocal DAO payable / ECO
        # receivable for verified, unreimbursed advances.
        eco_held_cash_gross = money(intercompany["eco_held_dao_cash_before_obligations"])
        dao_accounts_payable_to_eco = money(intercompany["dao_accounts_payable_to_eco"])
        open_accrued_obligations = -outstanding_manual_accrual_liability(
            property_source_rows,
            prop,
            args.as_of,
        )
        yhome_cash_restriction = Decimal()
        for source_property, restriction in yhome_restrictions.items():
            if row_matches_property({"Property": source_property}, prop):
                yhome_cash_restriction = restriction
                break
        eco_held_other_restrictions = yhome_cash_restriction
        eco_held_unrestricted_cash_before_floor = (
            eco_held_cash_gross
            - open_accrued_obligations
            - eco_held_other_restrictions
        )
        eco_held_unrestricted_cash = max(
            Decimal(), eco_held_unrestricted_cash_before_floor
        )
        eco_cash_reconciliation_deficit = max(
            Decimal(), -eco_held_unrestricted_cash_before_floor
        )
        eco_funded_activity_pending_reciprocal_review = Decimal()
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
                "eco_attributed_account_activity": (
                    f"{eco_attributed_account_activity:.2f}"
                    if custody_source_ok
                    else None
                ),
                "eco_held_cash_gross": (
                    f"{eco_held_cash_gross:.2f}" if custody_source_ok else None
                ),
                "open_accrued_obligations": (
                    f"{open_accrued_obligations:.2f}" if custody_source_ok else None
                ),
                "eco_held_restricted_cash": (
                    f"{eco_held_other_restrictions:.2f}" if custody_source_ok else None
                ),
                "yhome_cash_settlement_restriction": (
                    f"{yhome_cash_restriction:.2f}" if custody_source_ok else None
                ),
                "eco_held_unrestricted_cash": (
                    f"{eco_held_unrestricted_cash:.2f}" if custody_source_ok else None
                ),
                "eco_cash_reconciliation_deficit": (
                    f"{eco_cash_reconciliation_deficit:.2f}"
                    if custody_source_ok
                    else None
                ),
                "dao_accounts_payable_to_eco": (
                    f"{dao_accounts_payable_to_eco:.2f}" if custody_source_ok else None
                ),
                "eco_accounts_receivable_from_dao": (
                    f"{dao_accounts_payable_to_eco:.2f}" if custody_source_ok else None
                ),
                "intercompany_payable_status": "ok" if custody_source_ok else "reconciliation_pending",
                "intercompany_source_mode": intercompany.get("source_mode"),
                "gross_eco_advances": intercompany.get("gross_eco_advances"),
                "gross_dao_cash_credits": intercompany.get("gross_dao_cash_credits"),
                "intercompany_included_row_count": intercompany.get("included_row_count"),
                "intercompany_excluded_row_count": intercompany.get("excluded_row_count"),
                "intercompany_monthly_breakdown": intercompany.get("monthly_breakdown"),
                "intercompany_category_breakdown": intercompany.get("category_breakdown"),
                "eco_funded_activity_pending_reciprocal_review": (
                    f"{eco_funded_activity_pending_reciprocal_review:.2f}"
                    if custody_source_ok
                    else None
                ),
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

    if not custody_source_ok:
        issues.append(
            "custody source lacks ECO bank-account ownership; use the ID-bearing "
            "baselane_source_transaction_index.csv"
        )
    status = "ok" if not issues and not unmapped_property_dao_accounts else "review_required"
    report = {
        "status": status,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "as_of": args.as_of.isoformat(),
        "ledger": str(args.ledger),
        "ledger_sha256": source_sha256,
        "custody_ledger": str(args.custody_ledger),
        "custody_ledger_sha256": custody_source_sha256,
        "custody_source_status": "ok" if custody_source_ok else "missing_account_ownership",
        "policy": {
            "internal_transfers_only": True,
            "coownership_combined_reserve_floor": f"{COOWNERSHIP_COMBINED_RESERVE_FLOOR:.2f}",
            "coownership_local_bank_float": f"{COOWNERSHIP_LOCAL_BANK_FLOAT:.2f}",
            "coownership_combined_reserve_components": [
                "positive_lofty_operating_reserve",
                "eco_held_spendable_dao_cash",
            ],
            "coownership_combined_floor_enforced_by": "baselane_lofty_transfer_requirements.py",
            "non_coownership_operations_float": f"{NON_COOWNERSHIP_OPERATING_FLOAT:.2f}",
            "non_coownership_properties": sorted(NON_COOWNERSHIP_PROPERTIES),
            "security_principal_is_restricted": True,
            "interest_belongs_to_eco": True,
            "aops_pnl_accrual_excluded_from_cash_basis": True,
            "candidate_excess_is_not_execution_authority": True,
            "negative_eco_attributed_activity_is_not_cash": True,
            "verified_negative_intercompany_position_is_dao_payable_to_eco": True,
            "eco_net_dao_funds_has_zero_floor": True,
            "dao_payable_requires_id_bearing_eco_cash_evidence": True,
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
        "intercompany_subledger": [
            intercompany_subledger[prop] for prop in sorted(intercompany_subledger)
        ],
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
        "eco_attributed_account_activity",
        "eco_held_cash_gross",
        "open_accrued_obligations",
        "eco_held_restricted_cash",
        "yhome_cash_settlement_restriction",
        "eco_held_unrestricted_cash",
        "eco_cash_reconciliation_deficit",
        "dao_accounts_payable_to_eco",
        "eco_accounts_receivable_from_dao",
        "intercompany_payable_status",
        "gross_eco_advances",
        "gross_dao_cash_credits",
        "eco_funded_activity_pending_reciprocal_review",
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
