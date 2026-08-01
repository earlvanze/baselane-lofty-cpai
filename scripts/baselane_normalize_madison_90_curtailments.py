#!/usr/bin/env python3
"""Normalize every mapped 90 Madison NOI curtailment to Mortgage Principal."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import sys
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "baselane-mcp" / "src"))

from baselane_mcp.transfers import run_graphql_via_cdp  # noqa: E402


BRIDGE = ROOT / "scripts" / "baselane_graphql_via_cdp.js"
CONFIG = ROOT / "config" / "madison_90_principal_curtailments.json"
REPORT_DIR = ROOT / "reports"
PIPELINE_LOCK = ROOT / "scripts" / ".baselane_source_pipeline.lock"
PROPERTY_ID = "31525"
TAG_ID = "20"
CENT = Decimal("0.01")


def graphql(payload: dict[str, Any]) -> dict[str, Any]:
    return run_graphql_via_cdp(
        payload,
        bridge_path=BRIDGE,
        workspace_root=ROOT,
        timeout=120,
    )


def money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(CENT)


def note_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or "")
    return str(value or "")


def mapped_specs() -> list[dict[str, Any]]:
    policy = json.loads(CONFIG.read_text(encoding="utf-8"))
    specs = []
    for row in policy["recognition_schedule"]:
        if not row.get("transaction_id"):
            continue
        month = str(row["month"])
        specs.append(
            {
                "transaction_id": str(row["transaction_id"]),
                "parent_id": (
                    str(row["bank_root_id"])
                    if row.get("bank_root_id") is not None
                    else None
                ),
                "recognition_month": month,
                "amount": str(money(row["amount"])),
                "merchant": f"90 Madison | approved {month} NOI principal curtailment",
            }
        )
    return specs


def query_rows(ids: list[str]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for start in range(0, len(ids), 5):
        batch = ids[start : start + 5]
        variables = {f"id{i}": row_id for i, row_id in enumerate(batch)}
        declarations = ", ".join(f"$id{i}: ID!" for i in range(len(batch)))
        fields = "\n".join(
            f"""
              r{i}: transactionById(id: $id{i}) {{
                id amount date merchantName propertyId tagId note parentId
                bankAccountId isDeleted pending
              }}
            """
            for i in range(len(batch))
        )
        data = graphql(
            {
                "operationName": "Madison90CurtailmentsById",
                "variables": variables,
                "query": f"query Madison90CurtailmentsById({declarations}) {{ {fields} }}",
            }
        )["data"]
        rows.update({str(row["id"]): row for row in data.values() if row})
    return rows


def desired_note(spec: dict[str, Any], row: dict[str, Any]) -> str:
    canonical = (
        f"AOPS-90-CURTAILMENT|recognition={spec['recognition_month']}|"
        f"amount={spec['amount']} | Approved 50% NOI principal curtailment; "
        f"bank-posted {row['date']}. Ordinary mortgage P&I remains ECO responsibility."
    )
    current = note_text(row.get("note"))
    manual_marker = "AOPS-90-MORTGAGE-MANUAL-COMPONENT|"
    if manual_marker in current:
        # Preserve the statement-backed composite-root evidence while making
        # the approved curtailment marker canonical for downstream CF logic.
        return f"{canonical} | {current[current.index(manual_marker):]}"
    return canonical


def build_plan() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    specs = mapped_specs()
    rows = query_rows([spec["transaction_id"] for spec in specs])
    issues: list[str] = []
    actions: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    for spec in specs:
        row = rows.get(spec["transaction_id"])
        if not row:
            issues.append(f"missing:{spec['transaction_id']}")
            continue
        if row.get("isDeleted") or row.get("pending"):
            issues.append(f"inactive:{spec['transaction_id']}")
        if money(row.get("amount")) != -money(spec["amount"]):
            issues.append(f"amount_mismatch:{spec['transaction_id']}:{row.get('amount')}")
        actual_parent_id = str(row.get("parentId") or "") or None
        if actual_parent_id != spec["parent_id"]:
            issues.append(f"parent_mismatch:{spec['transaction_id']}:{row.get('parentId')}")
        target_note = desired_note(spec, row)
        exact = (
            str(row.get("propertyId") or "") == PROPERTY_ID
            and str(row.get("tagId") or "") == TAG_ID
            and str(row.get("merchantName") or "") == spec["merchant"]
            and note_text(row.get("note")) == target_note
        )
        actions.append(
            {
                **spec,
                "date": str(row.get("date") or ""),
                "current_property_id": str(row.get("propertyId") or ""),
                "current_tag_id": str(row.get("tagId") or ""),
                "current_merchant": str(row.get("merchantName") or ""),
                "action": "none" if exact else "normalize",
            }
        )
        if not exact:
            updates.append(
                {
                    "id": spec["transaction_id"],
                    "propertyId": PROPERTY_ID,
                    "tagId": TAG_ID,
                    "merchantName": spec["merchant"],
                    "note": target_note,
                    "isReviewedByUser": True,
                }
            )
    return (
        {
            "scope": "all exact-ID mapped 90 Madison approved NOI principal curtailments",
            "policy": (
                "Only configured 50% NOI curtailments are 90 Madison Mortgage "
                "Principal; ordinary P&I remains ECO responsibility."
            ),
            "issues": issues,
            "actions": actions,
        },
        updates,
    )


def digest(public: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(public, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@contextmanager
def pipeline_lock(enabled: bool) -> Iterator[None]:
    if not enabled:
        yield
        return
    with PIPELINE_LOCK.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def mutate(updates: list[dict[str, Any]]) -> None:
    if not updates:
        return
    graphql(
        {
            "operationName": "NormalizeMadison90Curtailments",
            "variables": {"input": updates},
            "query": """
              mutation NormalizeMadison90Curtailments($input: [UpdateTransaction!]) {
                updateTransactions(input: $input) {
                  id amount date merchantName propertyId tagId note parentId
                }
              }
            """,
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--digest")
    args = parser.parse_args()
    if args.apply and not args.digest:
        parser.error("--apply requires --digest")

    with pipeline_lock(args.apply):
        public, updates = build_plan()
        plan_digest = digest(public)
        mode = "preview"
        if args.apply:
            if args.digest != plan_digest:
                raise RuntimeError(
                    f"live digest changed: expected {args.digest}, current {plan_digest}"
                )
            if public["issues"]:
                raise RuntimeError(f"refusing apply with issues: {public['issues']}")
            mutate(updates)
            public, updates = build_plan()
            if public["issues"] or updates:
                raise RuntimeError(f"post-apply verification failed: {public}")
            mode = "applied_and_verified"

    report = {
        "status": "ok" if not public["issues"] else "blocked",
        "mode": mode,
        "digest": plan_digest,
        **public,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / (
        "madison_90_curtailment_normalization_"
        + ("applied.json" if args.apply else "preview.json")
    )
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({**report, "report": str(path)}, indent=2))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
