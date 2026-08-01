#!/usr/bin/env python3
"""Audit every live/deleted NY mortgage root and split child in Baselane.

This is a read-only, manual evidence tool.  It intentionally loads all four
hidden/deleted states because a visible orphan can point at a hidden/deleted
split parent.  It never infers that a positive reversal is a duplicate: a bank
root is valid when its active child set reconciles to the root amount.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "baselane-mcp" / "src"))

from baselane_mcp.transfers import run_graphql_via_cdp  # noqa: E402
from coownership_mortgage_policy import (  # noqa: E402
    is_approved_madison_90_curtailment,
)


BRIDGE = ROOT / "scripts" / "baselane_graphql_via_cdp.js"
DEFAULT_REPORT = ROOT / "reports" / "ny_mortgage_graph_audit.json"
CENT = Decimal("0.01")

NY_PROPERTIES = {
    "31525": "90 Madison Ave",
    "72376": "82 Madison Ave",
    "31499": "88 Madison Ave",
    "33594": "724 3rd Ave",
    "60548": "84 Madison Ave",
    "63162": "86 Madison Ave",
    "91341": "9 Country Club Ln N",
}
LEGACY_PROPERTY_IDS = {"37648": "Mining Supply Company / legacy"}
NY_BANKS = {
    "82703": "84 operating",
    "98200": "84 reserves",
    "88616": "86 operating",
    "129026": "86 reserves",
    "89681": "88 operating",
    "138368": "88 reserves",
    "89680": "90 operating",
    "90520": "90 reserves",
    "56668": "724 operating",
    "63514": "724 reserves",
    "157260": "724 deposits",
    "133098": "9 Country Club operating",
    "165515": "9 Country Club reserves",
    "38968": "ECO legacy pooled",
}
MORTGAGE_MARKERS = (
    "mortgage",
    "principal",
    "curtail",
    "escrow",
    "citadel",
    "loansphere",
    "rushmore",
    "onity",
    "mortgagequestions",
    "newrez",
    "shellpoin",
    "freedom",
    "ubs",
)
PROPERTY_MARKERS = (
    "84 madison",
    "86 madison",
    "88 madison",
    "90 madison",
    "724 3rd",
    "9 country club",
)


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def note_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or "")
    return str(value or "")


def searchable(row: dict[str, Any]) -> str:
    return " ".join(
        str(value or "")
        for value in (
            row.get("merchantName"),
            row.get("description"),
            note_text(row.get("note")),
        )
    ).lower()


def identifying_text(row: dict[str, Any]) -> str:
    """Transaction identity fields only; narrative notes are not classification."""
    return " ".join(
        str(value or "")
        for value in (row.get("merchantName"), row.get("description"))
    ).lower()


def graphql(payload: dict[str, Any]) -> dict[str, Any]:
    return run_graphql_via_cdp(
        payload,
        bridge_path=BRIDGE,
        workspace_root=ROOT,
        timeout=180,
    )


def query_state(*, hidden: bool, deleted: bool) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        result = graphql(
            {
                "operationName": "NyMortgageGraphTransactions",
                "variables": {
                    "input": {
                        "sort": {"direction": "DESC", "field": "date"},
                        "filter": {"isHidden": hidden, "isDeleted": deleted},
                        "page": page,
                        "pageLimit": 1000,
                    }
                },
                "query": """
                  query NyMortgageGraphTransactions($input: SortsAndFilters) {
                    transactions(input: $input) {
                      total
                      data {
                        id amount date merchantName description propertyId tagId
                        bankAccountId note isManual isSplit parentId hidden
                        isDeleted pending
                      }
                    }
                  }
                """,
            }
        )["data"]["transactions"]
        batch = list(result.get("data") or [])
        rows.extend(batch)
        if not batch or len(rows) >= int(result.get("total") or 0):
            return rows
        page += 1


def query_transactions_by_id(ids: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    # Baselane's GraphQL gateway permits at most five aliases per request.
    for start in range(0, len(ids), 5):
        batch = ids[start : start + 5]
        variables = {f"id{index}": row_id for index, row_id in enumerate(batch)}
        declarations = ", ".join(f"$id{index}: ID!" for index in range(len(batch)))
        selections = "\n".join(
            f"""
              row{index}: transactionById(id: $id{index}) {{
                id amount date merchantName description propertyId tagId
                bankAccountId note isManual isSplit parentId hidden isDeleted
                pending
              }}
            """
            for index in range(len(batch))
        )
        data = graphql(
            {
                "operationName": "NyMortgageGraphTransactionsById",
                "variables": variables,
                "query": (
                    f"query NyMortgageGraphTransactionsById({declarations}) "
                    f"{{ {selections} }}"
                ),
            }
        )["data"]
        rows.extend(row for row in data.values() if row)
    return rows


def resolve_state_query_duplicates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resolve Baselane state-filter overlap with authoritative ID lookups.

    Baselane can return the same transaction from both the active and deleted
    state-filtered list queries.  Blindly keeping the last list result can make
    an active split child look deleted.  Re-query only IDs whose returned state
    differs, then keep one authoritative row per transaction.
    """
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["id"])].append(row)
    conflicts = sorted(
        row_id
        for row_id, copies in grouped.items()
        if len(
            {
                (
                    bool(copy.get("hidden")),
                    bool(copy.get("isDeleted")),
                    bool(copy.get("pending")),
                )
                for copy in copies
            }
        )
        > 1
    )
    authoritative = (
        {str(row["id"]): row for row in query_transactions_by_id(conflicts)}
        if conflicts
        else {}
    )
    return [
        authoritative.get(row_id, copies[0])
        for row_id, copies in grouped.items()
    ]


def active(row: dict[str, Any]) -> bool:
    return not row.get("hidden") and not row.get("isDeleted")


def extant(row: dict[str, Any]) -> bool:
    """Present upstream even when hidden as a native split parent."""
    return not row.get("isDeleted")


def ny_seed(row: dict[str, Any]) -> bool:
    property_id = str(row.get("propertyId") or "")
    bank_id = str(row.get("bankAccountId") or "")
    text = searchable(row)
    explicit = property_id in NY_PROPERTIES or any(
        marker in text for marker in PROPERTY_MARKERS
    )
    mortgage_like = any(marker in text for marker in MORTGAGE_MARKERS)
    # ECO legacy is a pooled account, so a servicer name alone does not make a
    # row NY. An NY property/name marker is still required there.
    dedicated_ny_bank = bank_id in NY_BANKS and bank_id != "38968"
    return mortgage_like and (explicit or dedicated_ny_bank)


def compact(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row.get("id") or ""),
        "date": str(row.get("date") or ""),
        "amount": str(money(row.get("amount"))),
        "merchant": str(row.get("merchantName") or ""),
        "description": str(row.get("description") or ""),
        "property_id": str(row.get("propertyId") or ""),
        "property": (
            NY_PROPERTIES.get(str(row.get("propertyId") or ""))
            or LEGACY_PROPERTY_IDS.get(str(row.get("propertyId") or ""))
        ),
        "tag_id": str(row.get("tagId") or ""),
        "bank_account_id": str(row.get("bankAccountId") or ""),
        "bank_account": NY_BANKS.get(str(row.get("bankAccountId") or "")),
        "parent_id": str(row.get("parentId") or ""),
        "manual": bool(row.get("isManual")),
        "split": bool(row.get("isSplit")),
        "hidden": bool(row.get("hidden")),
        "deleted": bool(row.get("isDeleted")),
        "pending": bool(row.get("pending")),
        "note": note_text(row.get("note")),
    }


def build_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {str(row["id"]): row for row in rows}
    seed_rows = [row for row in rows if ny_seed(row)]
    missing_parent_ids = sorted(
        {
            str(row.get("parentId") or "")
            for row in seed_rows
            if row.get("parentId")
            and str(row.get("parentId")) not in by_id
        }
    )
    while missing_parent_ids:
        fetched = query_transactions_by_id(missing_parent_ids)
        by_id.update({str(row["id"]): row for row in fetched})
        next_ids = {
            str(row.get("parentId") or "")
            for row in fetched
            if row.get("parentId")
            and str(row.get("parentId")) not in by_id
        }
        missing_parent_ids = sorted(next_ids)
    rows = list(by_id.values())
    children: dict[str, list[dict[str, Any]]] = defaultdict(list)
    relevant_ids = {str(row["id"]) for row in seed_rows}
    for row in rows:
        parent_id = str(row.get("parentId") or "")
        if parent_id:
            children[parent_id].append(row)

    # Pull complete ancestors and descendants of every NY mortgage seed into
    # scope; legacy children are often assigned to the generic property.
    changed = True
    while changed:
        changed = False
        for row_id in list(relevant_ids):
            row = by_id.get(row_id)
            if row:
                parent_id = str(row.get("parentId") or "")
                if parent_id and parent_id not in relevant_ids:
                    relevant_ids.add(parent_id)
                    changed = True
            for child in children.get(row_id, []):
                child_id = str(child["id"])
                if child_id not in relevant_ids:
                    relevant_ids.add(child_id)
                    changed = True

    relevant = [by_id[row_id] for row_id in relevant_ids if row_id in by_id]
    active_relevant = [row for row in relevant if active(row)]

    active_orphans = []
    for row in active_relevant:
        parent_id = str(row.get("parentId") or "")
        if not parent_id:
            continue
        parent = by_id.get(parent_id)
        if parent is None or not extant(parent):
            active_orphans.append(
                {
                    "child": compact(row),
                    "parent": compact(parent) if parent else None,
                    "reason": "missing_parent" if parent is None else "inactive_parent",
                }
            )

    split_roots = []
    reconciliation_issues = []
    for parent_id, all_children in children.items():
        parent = by_id.get(parent_id)
        if not parent or parent_id not in relevant_ids or not extant(parent):
            continue
        active_children = [row for row in all_children if active(row)]
        if not active_children:
            continue
        extant_children = [row for row in all_children if extant(row)]
        active_child_sum = sum(
            (money(row.get("amount")) for row in active_children), Decimal("0.00")
        )
        extant_child_sum = sum(
            (money(row.get("amount")) for row in extant_children), Decimal("0.00")
        )
        active_difference = money(parent.get("amount")) - active_child_sum
        extant_difference = money(parent.get("amount")) - extant_child_sum
        record = {
            "parent": compact(parent),
            "active_child_count": len(active_children),
            "active_child_sum": str(active_child_sum.quantize(CENT)),
            "active_difference": str(active_difference.quantize(CENT)),
            "extant_child_count": len(extant_children),
            "extant_child_sum": str(extant_child_sum.quantize(CENT)),
            "extant_difference": str(extant_difference.quantize(CENT)),
            "active_children": [compact(row) for row in active_children],
            "inactive_children": [
                compact(row) for row in all_children if not active(row)
            ],
        }
        split_roots.append(record)
        if extant_difference != Decimal("0.00"):
            reconciliation_issues.append(record)

    # Exact active bankless/manual copies are candidates only; a duplicate is
    # not asserted unless a matching active split child also exists.
    active_children = [
        row for row in active_relevant if row.get("parentId") and active(by_id.get(str(row.get("parentId"))) or {})
    ]
    split_signatures: dict[tuple[str, Decimal, str], list[dict[str, Any]]] = defaultdict(list)
    for row in active_children:
        split_signatures[
            (str(row.get("date") or ""), money(row.get("amount")), str(row.get("tagId") or ""))
        ].append(row)
    standalone_duplicates = []
    for row in active_relevant:
        if row.get("parentId") or row.get("bankAccountId") or not row.get("isManual"):
            continue
        signature = (
            str(row.get("date") or ""),
            money(row.get("amount")),
            str(row.get("tagId") or ""),
        )
        matches = split_signatures.get(signature, [])
        if matches and (
            any(marker in searchable(row) for marker in MORTGAGE_MARKERS)
            or any(marker in searchable(match) for match in MORTGAGE_MARKERS for marker in MORTGAGE_MARKERS)
        ):
            standalone_duplicates.append(
                {
                    "standalone": compact(row),
                    "matching_active_children": [compact(match) for match in matches],
                }
            )

    curtailments = [
        compact(row)
        for row in relevant
        if is_approved_madison_90_curtailment(row)
    ]
    unsplit_roots = [
        compact(row)
        for row in relevant
        if active(row)
        and not row.get("parentId")
        and str(row.get("id") or "") not in children
        and any(marker in identifying_text(row) for marker in MORTGAGE_MARKERS)
    ]

    wrong_servicer_property = []
    servicer_expected = {
        "citadel": "31525",
        "loansphere": "31525",
        "rushmore": "60548",
        "onity": "63162",
        "mortgagequestions": "63162",
        "newrez": "31499",
        "shellpoin": "31499",
        "freedom": "33594",
    }
    for row in active_relevant:
        text = searchable(row)
        for marker, expected_property in servicer_expected.items():
            if marker not in text:
                continue
            actual = str(row.get("propertyId") or "")
            if actual not in {expected_property, "", "37648"}:
                wrong_servicer_property.append(
                    {
                        "row": compact(row),
                        "servicer_marker": marker,
                        "expected_property_id": expected_property,
                        "expected_property": NY_PROPERTIES[expected_property],
                    }
                )

    return {
        "generated_at": iso_z(),
        "status": "ok" if not active_orphans and not reconciliation_issues else "review",
        "policy": (
            "One real bank root and one active canonical component set. "
            "Positive reversals remain valid when their children reconcile. "
            "90 Madison principal-only curtailments are audited separately."
        ),
        "all_state_row_count": len(rows),
        "relevant_row_count": len(relevant),
        "active_relevant_row_count": len(active_relevant),
        "active_orphan_count": len(active_orphans),
        "active_orphans": sorted(active_orphans, key=lambda item: item["child"]["id"]),
        "active_split_root_count": len(split_roots),
        "reconciliation_issue_count": len(reconciliation_issues),
        "reconciliation_issues": sorted(
            reconciliation_issues, key=lambda item: item["parent"]["id"]
        ),
        "standalone_duplicate_candidate_count": len(standalone_duplicates),
        "standalone_duplicate_candidates": sorted(
            standalone_duplicates, key=lambda item: item["standalone"]["id"]
        ),
        "wrong_servicer_property_count": len(wrong_servicer_property),
        "wrong_servicer_property": sorted(
            wrong_servicer_property, key=lambda item: item["row"]["id"]
        ),
        "madison_90_curtailment_count": len(curtailments),
        "madison_90_curtailments": sorted(
            curtailments, key=lambda item: (item["date"], item["id"])
        ),
        "active_unsplit_mortgage_root_count": len(unsplit_roots),
        "active_unsplit_mortgage_roots": sorted(
            unsplit_roots, key=lambda item: (item["date"], item["id"])
        ),
        "split_roots": sorted(split_roots, key=lambda item: item["parent"]["id"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    rows = [
        row
        for hidden in (False, True)
        for deleted in (False, True)
        for row in query_state(hidden=hidden, deleted=deleted)
    ]
    report = build_report(resolve_state_query_duplicates(rows))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "status",
                    "all_state_row_count",
                    "relevant_row_count",
                    "active_orphan_count",
                    "reconciliation_issue_count",
                    "standalone_duplicate_candidate_count",
                    "wrong_servicer_property_count",
                    "madison_90_curtailment_count",
                    "active_unsplit_mortgage_root_count",
                )
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
