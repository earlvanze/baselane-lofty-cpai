#!/usr/bin/env python3
"""Reconcile no-DAO mortgage rows to cash-basis transfer treatment.

Real bank transactions remain property-scoped and are categorized as
Transfers Between Accounts. Manual mortgage escrow accruals are soft-deleted.
The default mode is preview; live application requires the exact preview digest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

import fcntl

from coownership_mortgage_policy import is_approved_madison_90_curtailment


ROOT = Path(os.environ.get("OPENCLAW_WORKSPACE_ROOT") or Path(__file__).absolute().parents[1])
GRAPHQL_HELPER = ROOT / "scripts" / "baselane_graphql_via_cdp.js"
PIPELINE_LOCK = ROOT / "scripts" / ".baselane_source_pipeline.lock"
DEFAULT_REPORT = ROOT / "reports" / "no_dao_mortgage_cash_basis_live.json"
TRANSFER_TAG_ID = "24"
MORTGAGE_TAG_IDS = {
    "8", "9", "11", "15", "20", "27", "33", "56", "65", "93", "95", "104", "130"
}
ESCROW_TAG_IDS = {"8", "15", "65", "95", "130"}
NO_DAO_PROPERTY_IDS = {
    "63162": "86 Madison Ave",
    "31499": "88 Madison Ave",
    "31525": "90 Madison Ave",
    "33594": "724 3rd Ave",
}
MORTGAGE_MARKERS = (
    "mortgage",
    "mtg ",
    "mtge ",
    "loandepot",
    "freedom",
    "newrez",
    "shellpoin",
    "citadel serv",
)


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalized(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def note_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or "")
    return str(value or "")


def amount_text(value: Any) -> str:
    return str(Decimal(str(value or "0")).quantize(Decimal("0.01")))


def run_graphql(payload: dict[str, Any]) -> dict[str, Any]:
    timeout_ms = int(os.environ.get("BASELANE_GQL_TIMEOUT_MS") or "90000")
    timeout_seconds = max(60, (timeout_ms + 30000) // 1000)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle)
        payload_path = Path(handle.name)
    try:
        completed = subprocess.run(
            ["node", str(GRAPHQL_HELPER), str(payload_path)],
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
        )
    finally:
        payload_path.unlink(missing_ok=True)
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
    result = json.loads(completed.stdout)
    if result.get("errors"):
        raise RuntimeError(json.dumps(result["errors"], sort_keys=True))
    return result


def query_transactions() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = {
            "operationName": "Transactions",
            "variables": {
                "input": {
                    "sort": {"direction": "DESC", "field": "date"},
                    "filter": {"isHidden": False, "isDeleted": False},
                    "page": page,
                    "pageLimit": 1000,
                }
            },
            "query": """
            query Transactions($input: SortsAndFilters) {
              transactions(input: $input) {
                total
                data {
                  id amount date merchantName propertyId tagId note isManual
                  isSplit parentId hidden isDeleted
                }
              }
            }
            """,
        }
        result = run_graphql(payload)["data"]["transactions"]
        batch = result.get("data") or []
        rows.extend(batch)
        total = int(result.get("total") or 0)
        if not batch or len(rows) >= total:
            return rows
        page += 1


def classify_action(
    row: dict[str, Any],
    parent_property_ids: dict[str, str] | None = None,
) -> str | None:
    parent_property_ids = parent_property_ids or {}
    property_id = str(row.get("propertyId") or "")
    parent_id = str(row.get("parentId") or "")
    if property_id not in NO_DAO_PROPERTY_IDS and parent_id not in parent_property_ids:
        return None
    if str(row.get("date") or "") < "2025-01-01":
        return None
    if row.get("hidden") or row.get("isDeleted"):
        return None

    merchant = normalized(row.get("merchantName"))
    searchable = normalized(
        " ".join((str(row.get("merchantName") or ""), note_text(row.get("note"))))
    )
    mortgage_derived = (
        any(marker in searchable for marker in MORTGAGE_MARKERS)
        or parent_id in parent_property_ids
    )
    manual_escrow = (
        bool(row.get("isManual"))
        and not parent_id
        and (
            merchant.startswith("escrow -")
            or "mortgage escrow" in searchable
        )
    )
    native_escrow_component = bool(parent_id) and (
        str(row.get("tagId") or "") in ESCROW_TAG_IDS
        or "escrow" in merchant
        or "mortgage insurance" in merchant
        or "property tax" in merchant
    )
    if native_escrow_component:
        return None
    if (
        property_id == "31525"
        and is_approved_madison_90_curtailment(row)
    ):
        return None
    if manual_escrow and str(row.get("tagId") or "") in MORTGAGE_TAG_IDS:
        return "delete_manual_escrow"
    if (
        mortgage_derived
        and str(row.get("tagId") or "") in MORTGAGE_TAG_IDS
        and str(row.get("tagId") or "") != TRANSFER_TAG_ID
    ):
        return "reclassify_transfer"
    return None


def action_record(
    row: dict[str, Any],
    action: str,
    target_property_id: str,
) -> dict[str, Any]:
    return {
        "action": action,
        "id": str(row["id"]),
        "date": str(row.get("date") or ""),
        "amount": amount_text(row.get("amount")),
        "merchant": str(row.get("merchantName") or ""),
        "source_property_id": str(row.get("propertyId") or ""),
        "property_id": target_property_id,
        "property": NO_DAO_PROPERTY_IDS[target_property_id],
        "tag_id": str(row.get("tagId") or ""),
        "parent_id": str(row.get("parentId") or ""),
        "is_manual": bool(row.get("isManual")),
    }


def build_actions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parent_property_ids = {
        str(row["id"]): str(row.get("propertyId") or "")
        for row in rows
        if row.get("isSplit")
        and str(row.get("propertyId") or "") in NO_DAO_PROPERTY_IDS
        and (
            any(
                marker in normalized(
                    f"{row.get('merchantName', '')} {note_text(row.get('note'))}"
                )
                for marker in MORTGAGE_MARKERS
            )
            or normalized(row.get("merchantName")).startswith("escrow -")
        )
    }
    parent_property_ids.update(
        {
            str(row.get("parentId")): str(row.get("propertyId"))
            for row in rows
            if row.get("parentId")
            and str(row.get("propertyId") or "") in NO_DAO_PROPERTY_IDS
            and str(row.get("tagId") or "") in MORTGAGE_TAG_IDS
            and (
                any(
                    marker in normalized(
                        f"{row.get('merchantName', '')} {note_text(row.get('note'))}"
                    )
                    for marker in MORTGAGE_MARKERS
                )
                or normalized(row.get("merchantName")).startswith("escrow -")
            )
        }
    )
    actions = [
        action_record(
            row,
            action,
            parent_property_ids.get(
                str(row.get("parentId") or ""),
                str(row.get("propertyId") or ""),
            ),
        )
        for row in rows
        for action in [classify_action(row, parent_property_ids)]
        if action
    ]
    return sorted(actions, key=lambda item: (item["action"], item["id"]))


def action_digest(actions: list[dict[str, Any]]) -> str:
    payload = json.dumps(actions, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def mutation_inputs(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    inputs = []
    for action in actions:
        item: dict[str, Any] = {"id": action["id"], "isReviewedByUser": True}
        if action["action"] == "delete_manual_escrow":
            item["isDeleted"] = True
        else:
            item["tagId"] = TRANSFER_TAG_ID
            item["propertyId"] = action["property_id"]
        inputs.append(item)
    return inputs


def update_transactions(inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for start in range(0, len(inputs), 100):
        payload = {
            "operationName": "UpdateTransactions",
            "variables": {"input": inputs[start : start + 100]},
            "query": """
            mutation UpdateTransactions($input: [UpdateTransaction!]) {
              updateTransactions(input: $input) {
                id amount date merchantName propertyId tagId isManual isDeleted parentId
              }
            }
            """,
        }
        results.extend(run_graphql(payload)["data"]["updateTransactions"])
    return results


@contextmanager
def exclusive_pipeline_lock() -> Iterator[None]:
    PIPELINE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with PIPELINE_LOCK.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def verify_actions(actions: list[dict[str, Any]], live_rows: list[dict[str, Any]]) -> list[str]:
    by_id = {str(row.get("id") or ""): row for row in live_rows}
    failures: list[str] = []
    for action in actions:
        row = by_id.get(action["id"])
        if action["action"] == "delete_manual_escrow":
            if row is not None:
                failures.append(f"manual_escrow_still_active:{action['id']}")
            continue
        if row is None:
            failures.append(f"reclassified_row_missing:{action['id']}")
            continue
        if str(row.get("tagId") or "") != TRANSFER_TAG_ID:
            failures.append(f"transfer_tag_mismatch:{action['id']}")
        if str(row.get("propertyId") or "") != action["property_id"]:
            failures.append(f"property_changed:{action['id']}")
        if amount_text(row.get("amount")) != action["amount"]:
            failures.append(f"amount_changed:{action['id']}")
    return failures


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--require-action-digest")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    if args.apply and not args.require_action_digest:
        parser.error("--apply requires --require-action-digest")

    applied: list[dict[str, Any]] = []
    verification_failures: list[str] = []
    if args.apply:
        with exclusive_pipeline_lock():
            actions = build_actions(query_transactions())
            digest = action_digest(actions)
            if digest != args.require_action_digest:
                raise SystemExit(
                    f"action digest changed: expected {args.require_action_digest}, current {digest}"
                )
            applied = update_transactions(mutation_inputs(actions))
            verification_failures = verify_actions(actions, query_transactions())
    else:
        actions = build_actions(query_transactions())
        digest = action_digest(actions)

    report = {
        "generated_at": iso_z(),
        "status": "ok" if not verification_failures else "failed",
        "mode": "apply" if args.apply else "preview",
        "policy": (
            "Preserve real no-DAO mortgage cash movements as property-scoped Transfers Between "
            "Accounts; delete only manual mortgage escrow accruals."
        ),
        "action_digest": digest,
        "action_count": len(actions),
        "delete_manual_escrow_count": sum(
            item["action"] == "delete_manual_escrow" for item in actions
        ),
        "reclassify_transfer_count": sum(
            item["action"] == "reclassify_transfer" for item in actions
        ),
        "actions": actions,
        "applied_count": len(applied),
        "verification_failures": verification_failures,
    }
    write_report(args.report, report)
    print(json.dumps({key: report[key] for key in (
        "status", "mode", "action_digest", "action_count",
        "delete_manual_escrow_count", "reclassify_transfer_count",
        "applied_count", "verification_failures",
    )}, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
