#!/usr/bin/env python3
"""Audit and normalize ECO <-> 86/88/90 Madison cash-transfer metadata.

All bank-backed internal-transfer parents and split children remain category 24
(`Transfers Between Accounts`).  Month and accounting-purpose detail lives in
merchant labels and notes only, so the native splits are auditable without
creating false income or expenses in downstream cash-flow statements.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
GRAPHQL_HELPER = ROOT / "scripts" / "baselane_graphql_via_cdp.js"
REPORT_DIR = ROOT / "reports"
TAG_TRANSFER = "24"
CENT = Decimal("0.01")

ECO_BANK = "38968"
PROPERTY_BY_DAO_BANK = {
    "88616": "63162",  # 86 Madison
    "89681": "31499",  # 88 Madison
    "89680": "31525",  # 90 Madison
}
PROPERTY_BY_DAO_ENTITY = {
    "snow leopard": "63162",
    "heron": "31499",
    "strawberry": "31525",
}
PROPERTY_LABEL = {
    "63162": "86 Madison",
    "31499": "88 Madison",
    "31525": "90 Madison",
}


def cents(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def note_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or "")
    return str(value or "")


def run_graphql(payload: dict[str, Any]) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8") as handle:
        json.dump(payload, handle)
        handle.flush()
        proc = subprocess.run(
            ["node", str(GRAPHQL_HELPER), handle.name],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
        )
    if proc.returncode:
        raise RuntimeError(f"GraphQL helper failed: {proc.stderr[-1800:]}")
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    if result.get("errors"):
        raise RuntimeError(f"GraphQL errors: {json.dumps(result['errors'])[:2200]}")
    return result


def query_tag(tag_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        result = run_graphql(
            {
                "operationName": "Transactions",
                "variables": {
                    "input": {
                        "sort": {"field": "date", "direction": "DESC"},
                        "filter": {
                            "isHidden": False,
                            "search": "",
                            "isCategorized": None,
                            "tagId": tag_id,
                            "bankAccountId": None,
                            "propertyId": None,
                            "unitId": None,
                            "isDeleted": False,
                            "isDocumentUploaded": None,
                        },
                        "page": page,
                        "pageLimit": 1000,
                    }
                },
                "query": """
                query Transactions($input: SortsAndFilters) {
                  transactions(input: $input) {
                    total
                    data {
                      id amount date merchantName propertyId tagId bankAccountId
                      note isManual hidden isDeleted isSplit parentId
                    }
                  }
                }
                """,
            }
        )["data"]["transactions"]
        batch = result["data"]
        rows.extend(batch)
        if len(rows) >= int(result["total"]) or not batch:
            return rows
        page += 1


def query_parents(parent_ids: list[str]) -> dict[str, dict[str, Any]]:
    parents: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(parent_ids), 5):
        batch = parent_ids[offset : offset + 5]
        fields = "\n".join(
            f"""
            t{index}: transactionById(id: "{parent_id}") {{
              id amount date merchantName propertyId tagId bankAccountId note
              isSplit isDeleted parentId
              splitTransactions {{
                id amount date merchantName propertyId tagId parentId isDeleted
              }}
            }}
            """
            for index, parent_id in enumerate(batch)
        )
        data = run_graphql(
            {
                "operationName": "MadisonCashParents",
                "variables": {},
                "query": f"query MadisonCashParents {{ {fields} }}",
            }
        )["data"]
        parents.update({str(row["id"]): row for row in data.values() if row})
    return parents


def active_children(parent: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in parent.get("splitTransactions") or [] if not row.get("isDeleted")]


def expected_children(
    parent: dict[str, Any],
    property_id: str,
    components: list[tuple[str, Decimal]],
) -> list[dict[str, Any]]:
    amount = cents(parent["amount"])
    sign = Decimal("-1") if amount < 0 else Decimal("1")
    rows = [
        {
            "amount": cents(sign * component_amount),
            "date": str(parent["date"]),
            "merchantName": label,
            "propertyId": property_id,
            "tagId": TAG_TRANSFER,
        }
        for label, component_amount in components
    ]
    if sum((row["amount"] for row in rows), Decimal("0")) != amount:
        raise ValueError(
            f"split components for {parent['id']} sum to "
            f"{sum((row['amount'] for row in rows), Decimal('0'))}, not {amount}"
        )
    return rows


def normalized(rows: list[dict[str, Any]]) -> list[tuple[Decimal, str, str, str, str]]:
    return sorted(
        (
            cents(row.get("amount") or 0),
            str(row.get("date") or ""),
            str(row.get("merchantName") or ""),
            str(row.get("propertyId") or ""),
            str(row.get("tagId") or ""),
        )
        for row in rows
    )


def split_parent(parent_id: str, children: list[dict[str, Any]]) -> dict[str, Any]:
    return run_graphql(
        {
            "operationName": "createOrUpdateSplitTx",
            "variables": {
                "parentTransactionId": parent_id,
                "splitType": "AMOUNT",
                "transactionSplitInputs": [
                    {
                        **row,
                        "amount": float(row["amount"]),
                        "propertyUnitId": None,
                    }
                    for row in children
                ],
            },
            "query": """
            mutation createOrUpdateSplitTx(
              $parentTransactionId: ID!
              $splitType: SplitType!
              $transactionSplitInputs: [TransactionSplitInput!]!
            ) {
              createOrUpdateSplitTx(input: {
                parentTransactionId: $parentTransactionId
                transactionSplitInputs: $transactionSplitInputs
                splitType: $splitType
              }) {
                id
                splitTransactions {
                  id amount date merchantName propertyId tagId parentId isDeleted
                }
              }
            }
            """,
        }
    )["data"]["createOrUpdateSplitTx"]


def reconcile_inputs(parent: dict[str, Any], target: list[dict[str, Any]]) -> list[dict[str, Any]]:
    existing = active_children(parent)
    by_key: dict[tuple[Decimal, str, str, str, str], list[dict[str, Any]]] = {}
    for child in existing:
        by_key.setdefault(normalized([child])[0], []).append(child)

    inputs: list[dict[str, Any]] = []
    kept_ids: set[str] = set()
    for target_row in target:
        candidates = sorted(
            by_key.get(normalized([target_row])[0], []),
            key=lambda row: int(str(row.get("id") or "0")),
            reverse=True,
        )
        row = dict(target_row)
        if candidates:
            row["id"] = str(candidates[0]["id"])
            kept_ids.add(str(candidates[0]["id"]))
        inputs.append(row)

    for child in existing:
        child_id = str(child.get("id") or "")
        if child_id in kept_ids:
            continue
        inputs.append(
            {
                "id": child_id,
                "amount": cents(child.get("amount") or 0),
                "date": str(child.get("date") or parent.get("date") or ""),
                "merchantName": str(child.get("merchantName") or ""),
                "propertyId": str(child.get("propertyId") or ""),
                "tagId": str(child.get("tagId") or TAG_TRANSFER),
                "isDelete": True,
            }
        )
    return inputs


def reconcile_parent_split(parent: dict[str, Any], target: list[dict[str, Any]]) -> dict[str, Any]:
    return split_parent(str(parent["id"]), reconcile_inputs(parent, target))


def batch_split_actions(
    actions: list[dict[str, Any]],
    parents: dict[str, dict[str, Any]],
) -> list[str]:
    """Apply up to five independent split parents in one guarded GraphQL mutation."""
    if not actions:
        return []
    if len(actions) > 5:
        raise ValueError("batch_split_actions accepts at most five parents")
    variables: dict[str, Any] = {}
    declarations: list[str] = []
    fields: list[str] = []
    applied: list[str] = []
    for index, action in enumerate(actions):
        parent_id = str(action["parent_id"])
        inputs = (
            action["_target"]
            if action["action"] == "create"
            else reconcile_inputs(parents[parent_id], action["_target"])
        )
        variables[f"p{index}"] = parent_id
        variables[f"i{index}"] = [
            {
                **row,
                "amount": float(row["amount"]),
                "propertyUnitId": None,
            }
            for row in inputs
        ]
        declarations.extend(
            [
                f"$p{index}: ID!",
                f"$i{index}: [TransactionSplitInput!]!",
            ]
        )
        fields.append(
            f"""
            s{index}: createOrUpdateSplitTx(input: {{
              parentTransactionId: $p{index}
              transactionSplitInputs: $i{index}
              splitType: AMOUNT
            }}) {{
              id
              splitTransactions {{
                id amount date merchantName propertyId tagId parentId isDeleted
              }}
            }}
            """
        )
        applied.append(parent_id)
    run_graphql(
        {
            "operationName": "BatchMadisonTransferSplits",
            "variables": variables,
            "query": (
                f"mutation BatchMadisonTransferSplits({', '.join(declarations)}) "
                f"{{ {' '.join(fields)} }}"
            ),
        }
    )
    return applied


def update_transactions(inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not inputs:
        return []
    result: list[dict[str, Any]] = []
    for offset in range(0, len(inputs), 50):
        batch = inputs[offset : offset + 50]
        result.extend(
            run_graphql(
                {
                    "operationName": "UpdateTransaction",
                    "variables": {"input": batch},
                    "query": """
                    mutation UpdateTransaction($input: [UpdateTransaction!]) {
                      updateTransactions(input: $input) {
                        id amount date merchantName propertyId tagId note
                      }
                    }
                    """,
                }
            )["data"]["updateTransactions"]
        )
    return result


def split_specs() -> dict[str, dict[str, Any]]:
    specs: dict[str, dict[str, Any]] = {}

    def add(
        parent_ids: tuple[str, ...],
        property_id: str,
        note: str,
        components: list[tuple[str, str]],
    ) -> None:
        parsed = [(label, Decimal(amount)) for label, amount in components]
        for parent_id in parent_ids:
            specs[parent_id] = {
                "property_id": property_id,
                "note": note,
                "components": parsed,
            }

    # PM-fee cash transfers.  These amounts allocate physical cash only; the
    # paired manual accruals remain the sole P&L recognition entries.
    add(
        ("150135632",),
        "63162",
        "86 Madison PM cash transfer: Jan-Mar 2025, native components; all components are Transfers Between Accounts.",
        [
            ("86-ECO | PM cash | 2025-01", "448.83"),
            ("86-ECO | PM cash | 2025-02", "785.39"),
            ("86-ECO | PM cash | 2025-03", "1258.43"),
        ],
    )
    add(
        ("187892751",),
        "31499",
        "88 Madison composite cash transfer: Aug 2025 PM plus short-term-loan residual; labels are audit-only.",
        [
            ("88-ECO | PM cash | 2025-08", "2328.96"),
            ("88-ECO | short-term loan | residual", "1671.04"),
        ],
    )
    add(
        ("229680612",),
        "63162",
        "86 Madison partial Dec 2025 PM cash transfer; $233.46 remained unpaid at this transfer date.",
        [("86-ECO | PM cash | 2025-12 | partial", "1000.00")],
    )
    add(
        ("319780172", "319780176"),
        "63162",
        "86 Madison $3,000 PM cash settlement allocated to open monthly accruals; all native components remain Transfers Between Accounts.",
        [
            ("86-ECO | PM cash | 2025-07 | remaining", "848.12"),
            ("86-ECO | PM cash | 2025-08 | remaining", "203.66"),
            ("86-ECO | PM cash | 2025-12 | remaining", "233.46"),
            ("86-ECO | PM cash | 2026-02", "863.24"),
            ("86-ECO | PM cash | 2026-03 | partial", "851.52"),
        ],
    )

    # 86 Madison mortgage funding, split into obligation months/categories.
    add(
        ("150135445", "150135469"),
        "63162",
        "86 Madison mortgage funding for Feb-Apr 2025; each monthly component includes $1,167.41 P&I and $142.67 escrow.",
        [
            ("86-ECO | mortgage | 2025-02 | P&I 1167.41 + escrow 142.67", "1310.08"),
            ("86-ECO | mortgage | 2025-03 | P&I 1167.41 + escrow 142.67", "1310.08"),
            ("86-ECO | mortgage | 2025-04 | P&I 1167.41 + escrow 142.67", "1310.08"),
        ],
    )
    add(
        ("159948664", "159948684"),
        "63162",
        "86 Madison May 2025 mortgage funding split between P&I and general escrow.",
        [
            ("86-ECO | mortgage | 2025-05 | P&I", "1167.41"),
            ("86-ECO | mortgage | 2025-05 | general escrow", "135.44"),
        ],
    )
    add(
        ("241096354", "241096372"),
        "63162",
        "86 Madison mortgage P&I funding for Dec 2025-Feb 2026, split by obligation month.",
        [
            ("86-ECO | mortgage P&I | 2025-12", "1167.41"),
            ("86-ECO | mortgage P&I | 2026-01", "1167.41"),
            ("86-ECO | mortgage P&I | 2026-02", "1167.41"),
        ],
    )

    # 88 Madison mortgage funding.  PMI/general escrow is DAO-benefit cash,
    # but still a transfer component rather than an expense on the cash row.
    add(
        ("150145163", "150145188"),
        "31499",
        "88 Madison January 2025 mortgage funding split between P&I and PMI/general escrow.",
        [
            ("88-ECO | mortgage | 2025-01 | P&I", "1082.78"),
            ("88-ECO | mortgage | 2025-01 | PMI/general escrow", "247.25"),
        ],
    )
    for parent_ids, month in [
        (("150145389", "150145441"), "2025-03"),
        (("156155609", "156155625"), "2025-04"),
        (("254364841", "254364856"), "2025-05"),
    ]:
        add(
            parent_ids,
            "31499",
            f"88 Madison {month} mortgage funding split between P&I and PMI/general escrow.",
            [
                (f"88-ECO | mortgage | {month} | P&I", "1082.78"),
                (f"88-ECO | mortgage | {month} | PMI/general escrow", "200.18"),
            ],
        )
    add(
        ("254365094", "254365106"),
        "31499",
        "88 Madison mortgage P&I funding for Jun-Nov 2025, split by obligation month.",
        [
            (f"88-ECO | mortgage P&I | 2025-{month:02d}", "1082.78")
            for month in range(6, 12)
        ],
    )

    # 90 Madison mortgage funding.
    add(
        ("149684884", "149684890"),
        "31525",
        "90 Madison April 2025 mortgage funding split into original statement components; all remain Transfers Between Accounts.",
        [
            ("90-ECO | mortgage | 2025-04 | interest", "1568.33"),
            ("90-ECO | mortgage | 2025-04 | principal", "200.88"),
            ("90-ECO | mortgage | 2025-04 | general escrow", "261.01"),
            ("90-ECO | mortgage | 2025-04 | NSF fee", "20.00"),
        ],
    )
    add(
        ("241096820", "241096839"),
        "31525",
        "90 Madison mortgage P&I funding for Jan-Feb 2026, split by obligation month.",
        [
            ("90-ECO | mortgage P&I | 2026-01", "1769.21"),
            ("90-ECO | mortgage P&I | 2026-02", "1769.21"),
        ],
    )
    return specs


def target_property_for_eco_dao_transfer(row: dict[str, Any]) -> str | None:
    bank_id = str(row.get("bankAccountId") or "")
    merchant = str(row.get("merchantName") or "").lower()
    if bank_id in PROPERTY_BY_DAO_BANK and merchant.startswith("eco systems"):
        return PROPERTY_BY_DAO_BANK[bank_id]
    if bank_id == ECO_BANK:
        for entity_fragment, property_id in PROPERTY_BY_DAO_ENTITY.items():
            if entity_fragment in merchant:
                return property_id
    return None


def is_pm_cash_candidate(row: dict[str, Any]) -> bool:
    if str(row.get("tagId") or "") != "80":
        return False
    property_id = target_property_for_eco_dao_transfer(row)
    if not property_id:
        return False
    text = f"{row.get('merchantName') or ''} {note_text(row.get('note'))}".lower()
    return "internal_transfer" in text and ("pm fee" in text or "property management" in text)


def is_mortgage_cash_candidate(row: dict[str, Any]) -> bool:
    if str(row.get("tagId") or "") not in {"24", "33"}:
        return False
    if not target_property_for_eco_dao_transfer(row):
        return False
    text = f"{row.get('merchantName') or ''} {note_text(row.get('note'))}".lower()
    if "internal_transfer" not in text:
        return False
    if "eco cleanup: co-owner mortgage payment" in text:
        return False
    return any(token in text for token in ("mortgage", "principal", "interest", "escrow"))


def public_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "date": str(row.get("date") or ""),
        "amount": str(cents(row.get("amount") or 0)),
        "bank_account_id": str(row.get("bankAccountId") or ""),
        "old_property_id": str(row.get("propertyId") or ""),
        "old_tag_id": str(row.get("tagId") or ""),
        "note": note_text(row.get("note")),
    }


def build_plan() -> tuple[dict[str, Any], dict[str, Any]]:
    specs = split_specs()
    tag_rows = query_tag("80") + query_tag("33") + query_tag("24")
    by_id = {str(row["id"]): row for row in tag_rows}
    parents = query_parents(sorted(specs))
    missing = sorted(set(specs) - set(parents))
    issues: list[str] = []
    if missing:
        issues.append(f"missing split parents: {missing}")

    scheduled_child_ids = {
        str(child["id"])
        for parent in parents.values()
        for child in active_children(parent)
    }

    metadata_updates: list[dict[str, Any]] = []
    metadata_public: list[dict[str, Any]] = []
    seen_updates: set[str] = set()
    candidates = [
        row
        for row in by_id.values()
        if is_pm_cash_candidate(row) or is_mortgage_cash_candidate(row)
    ]
    for row in sorted(candidates, key=lambda item: int(str(item["id"]))):
        row_id = str(row["id"])
        if row_id in scheduled_child_ids:
            continue
        parent_id = str(row.get("parentId") or "")
        if parent_id:
            issues.append(
                f"candidate child {row_id} belongs to unscheduled split parent {parent_id}"
            )
            continue
        property_id = target_property_for_eco_dao_transfer(row)
        assert property_id
        if (
            str(row.get("tagId") or "") == TAG_TRANSFER
            and str(row.get("propertyId") or "") == property_id
        ):
            continue
        update = {"id": row_id, "propertyId": property_id, "tagId": TAG_TRANSFER}
        metadata_updates.append(update)
        metadata_public.append(
            {
                **public_row(row),
                "new_property_id": property_id,
                "new_tag_id": TAG_TRANSFER,
            }
        )
        seen_updates.add(row_id)

    split_actions: list[dict[str, Any]] = []
    for parent_id, spec in sorted(specs.items(), key=lambda item: int(item[0])):
        parent = parents.get(parent_id)
        if not parent:
            continue
        if parent.get("isDeleted"):
            issues.append(f"split parent {parent_id} is deleted")
            continue
        target = expected_children(parent, spec["property_id"], spec["components"])
        existing = active_children(parent)
        exact = normalized(existing) == normalized(target)
        metadata_exact = (
            str(parent.get("tagId") or "") == TAG_TRANSFER
            and str(parent.get("propertyId") or "") == spec["property_id"]
            and note_text(parent.get("note")) == spec["note"]
        )
        split_actions.append(
            {
                "parent_id": parent_id,
                "date": str(parent.get("date") or ""),
                "amount": str(cents(parent.get("amount") or 0)),
                "property_id": spec["property_id"],
                "action": "already_exact" if exact else ("replace" if existing else "create"),
                "metadata_action": "already_exact" if metadata_exact else "update",
                "note": spec["note"],
                "components": [
                    {
                        "amount": str(row["amount"]),
                        "label": row["merchantName"],
                        "property_id": row["propertyId"],
                        "tag_id": row["tagId"],
                    }
                    for row in target
                ],
                "_target": target,
            }
        )
        if not metadata_exact and parent_id not in seen_updates:
            metadata_updates.append(
                {
                    "id": parent_id,
                    "propertyId": spec["property_id"],
                    "tagId": TAG_TRANSFER,
                    "note": spec["note"],
                }
            )
            metadata_public.append(
                {
                    **public_row(parent),
                    "new_property_id": spec["property_id"],
                    "new_tag_id": TAG_TRANSFER,
                    "new_note": spec["note"],
                }
            )
        elif not metadata_exact:
            for update in metadata_updates:
                if str(update["id"]) == parent_id:
                    update["propertyId"] = spec["property_id"]
                    update["tagId"] = TAG_TRANSFER
                    update["note"] = spec["note"]
                    break
            for row in metadata_public:
                if str(row["id"]) == parent_id:
                    row["new_property_id"] = spec["property_id"]
                    row["new_tag_id"] = TAG_TRANSFER
                    row["new_note"] = spec["note"]
                    break

    public_splits = [
        {key: value for key, value in row.items() if not key.startswith("_")}
        for row in split_actions
    ]
    public_plan = {
        "scope": "ECO <-> 86/88/90 Madison bank-backed internal cash transfers",
        "cash_flow_category_invariant": {
            "tag_id": TAG_TRANSFER,
            "label": "Transfers Between Accounts",
            "applies_to": "all targeted parents and active split children",
        },
        "issues": sorted(set(issues)),
        "metadata_updates": metadata_public,
        "split_actions": public_splits,
        "counts": {
            "metadata_updates": len(metadata_updates),
            "split_parents": len(split_actions),
            "split_creates_or_replacements": sum(
                row["action"] != "already_exact" for row in split_actions
            ),
        },
    }
    private = {
        "metadata_updates": metadata_updates,
        "split_actions": split_actions,
        "parents": parents,
    }
    return public_plan, private


def digest_plan(public_plan: dict[str, Any]) -> str:
    payload = json.dumps(public_plan, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def write_report(name: str, payload: dict[str, Any]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / name
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--digest", help="Required reviewed dry-run digest for --apply")
    args = parser.parse_args()

    public_plan, private = build_plan()
    digest = digest_plan(public_plan)
    dry_payload = {"status": "dry_run", "digest": digest, **public_plan}
    dry_path = write_report("madison_intercompany_cash_cleanup_dry_run.json", dry_payload)

    if not args.apply:
        print(json.dumps({**dry_payload, "report": str(dry_path)}, indent=2))
        return 0 if not public_plan["issues"] else 2

    if not args.digest or args.digest != digest:
        raise RuntimeError(
            f"live plan digest is {digest}; --apply requires the identical reviewed --digest"
        )
    if public_plan["issues"]:
        raise RuntimeError(f"refusing apply with issues: {public_plan['issues']}")

    updated = update_transactions(private["metadata_updates"])
    pending_split_actions = [
        action
        for action in private["split_actions"]
        if action["action"] in {"create", "replace"}
    ]
    split_applied: list[str] = []
    for offset in range(0, len(pending_split_actions), 5):
        split_applied.extend(
            batch_split_actions(
                pending_split_actions[offset : offset + 5],
                private["parents"],
            )
        )

    verify_public, _ = build_plan()
    remaining_split_actions = [
        row
        for row in verify_public["split_actions"]
        if row["action"] != "already_exact"
        or row["metadata_action"] != "already_exact"
    ]
    if (
        verify_public["issues"]
        or verify_public["metadata_updates"]
        or remaining_split_actions
    ):
        raise RuntimeError(
            "post-apply verification failed: "
            + json.dumps(
                {
                    "issues": verify_public["issues"],
                    "metadata_updates": verify_public["metadata_updates"],
                    "remaining_split_actions": remaining_split_actions,
                },
                indent=2,
            )
        )

    payload = {
        "status": "applied_and_verified",
        "reviewed_digest": digest,
        "metadata_rows_updated": [str(row["id"]) for row in updated],
        "split_parents_applied": split_applied,
        "verified_split_parent_count": len(verify_public["split_actions"]),
        "cash_flow_category_invariant": verify_public["cash_flow_category_invariant"],
    }
    report_path = write_report("madison_intercompany_cash_cleanup_apply.json", payload)
    print(json.dumps({**payload, "report": str(report_path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
