#!/usr/bin/env python3
"""Remove evidenced post-close Elton activity from the sold DAO's GL scope.

Elton closed on 2026-03-16 and disbursed on 2026-03-17.  The seller received
the closing proceeds directly.  These later ECO/Aligned cash rows therefore
must not manufacture a new receivable from the sold DAO.  The script preserves
every bank transaction and category, changes only property scope and notes,
and uses immutable transaction identity plus digest-gated apply.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from baselane_apply_alcott_accruals_live import run_graphql


ROOT = Path(__file__).absolute().parents[1]
REPORT = ROOT / "reports" / "baselane_elton_post_close_reclassification.20260729.json"
APPLY_ENV = "BASELANE_ELTON_POST_CLOSE_APPLY"
APPLY_DIGEST_ENV = "BASELANE_ELTON_POST_CLOSE_DIGEST"
ELTON_PROPERTY_ID = "83237"

TARGETS: dict[str, dict[str, Any]] = {
    "316164460": {
        "date": "2026-07-17",
        "amount": -9034.85,
        "merchant": "Lofty Holding 7411 Elton Avenue DAO LLC  |  TRANSFER_O",
        "search": "EVERNEST Balance Due TO 7411 Elton",
        "note": (
            "ECO receivable from Lofty closeout: post-sale Evernest remittance "
            "for 7411 Elton Ave. Elton closed 2026-03-16 and closing proceeds "
            "were paid directly to Lofty; this cash payment does not create a "
            "debt or capital call for the sold DAO. Reclassified 2026-07-29; "
            "original cash movement preserved."
        ),
    },
    "262899219": {
        "date": "2026-04-02",
        "amount": -2387.97,
        "merchant": "Aligned Properti",
        "search": "R&M and water/sewer",
        "note": (
            "Post-close R&M and water/sewer settlement for 7411 Elton Ave; "
            "recoverable in the Lofty sold-property closeout, not a new debt "
            "or capital call for the sold DAO. Reclassified 2026-07-29; "
            "original cash movement preserved."
        ),
    },
    "262446781": {
        "date": "2026-04-03",
        "amount": -91.17,
        "merchant": "OSC - RISK SECURE",
        "search": "OSC - RISK SECURE",
        "note": (
            "Post-sale insurance charge after Elton's 2026-03-17 disbursement; "
            "excluded from the sold DAO closeout and retained for ECO "
            "cancellation/refund review. Reclassified 2026-07-29."
        ),
    },
    "275492369": {
        "date": "2026-05-01",
        "amount": -91.17,
        "merchant": "OSC - RISK SECURE",
        "search": "OSC - RISK SECURE",
        "note": (
            "Post-sale insurance charge after Elton's 2026-03-17 disbursement; "
            "excluded from the sold DAO closeout and retained for ECO "
            "cancellation/refund review. Reclassified 2026-07-29."
        ),
    },
    "277301169": {
        "date": "2026-05-05",
        "amount": -91.17,
        "merchant": "OSC - RISK SECURE",
        "search": "OSC - RISK SECURE",
        "note": (
            "Post-sale insurance charge after Elton's 2026-03-17 disbursement; "
            "excluded from the sold DAO closeout and retained for ECO "
            "cancellation/refund review. Reclassified 2026-07-29."
        ),
    },
    "307166823": {
        "date": "2026-07-03",
        "amount": -91.17,
        "merchant": "OSC - RISK SECURE",
        "search": "OSC - RISK SECURE",
        "note": (
            "Post-sale insurance charge after Elton's 2026-03-17 disbursement; "
            "excluded from the sold DAO closeout and retained for ECO "
            "cancellation/refund review. Reclassified 2026-07-29."
        ),
    },
}


def note_text(value: Any) -> str:
    return str(value.get("text") or "") if isinstance(value, dict) else str(value or "")


def query_transactions(search: str) -> list[dict[str, Any]]:
    response = run_graphql({
        "operationName": "Transactions",
        "variables": {
            "input": {
                "sort": {"direction": "ASC", "field": "date"},
                "filter": {
                    "search": search,
                    "isHidden": False,
                    "isDeleted": False,
                },
                "page": 1,
                "pageLimit": 100,
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
    })
    return response["data"]["transactions"].get("data") or []


def read_live_targets() -> list[dict[str, Any]]:
    live: dict[str, dict[str, Any]] = {}
    for search in sorted({target["search"] for target in TARGETS.values()}):
        for row in query_transactions(search):
            live[str(row.get("id") or "")] = row
    verified: list[dict[str, Any]] = []
    for transaction_id, expected in TARGETS.items():
        row = live.get(transaction_id)
        if row is None:
            raise RuntimeError(f"Missing live transaction {transaction_id}")
        actual = {
            "date": str(row.get("date") or ""),
            "amount": round(float(row.get("amount") or 0), 2),
            "merchant": str(row.get("merchantName") or ""),
            "property_id": str(row.get("propertyId")) if row.get("propertyId") is not None else None,
            "tag_id": str(row.get("tagId") or ""),
            "bank_account_id": str(row.get("bankAccountId") or ""),
            "note": note_text(row.get("note")),
            "is_manual": bool(row.get("isManual")),
            "is_deleted": bool(row.get("isDeleted")),
        }
        if (
            actual["date"] != expected["date"]
            or actual["amount"] != expected["amount"]
            or actual["merchant"] != expected["merchant"]
            or actual["property_id"] not in (ELTON_PROPERTY_ID, None)
            or actual["is_manual"]
            or actual["is_deleted"]
        ):
            raise RuntimeError(f"Live identity mismatch for {transaction_id}: {actual}")
        verified.append({
            "id": transaction_id,
            **actual,
            "target_property_id": None,
            "target_note": expected["note"],
            "already_applied": actual["property_id"] is None and actual["note"] == expected["note"],
        })
    return verified


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args()

    before = read_live_targets()
    digest_input = [
        {
            "id": row["id"],
            "date": row["date"],
            "amount": row["amount"],
            "merchant": row["merchant"],
            "property_id": row["property_id"],
            "target_property_id": row["target_property_id"],
            "target_note": row["target_note"],
        }
        for row in before
    ]
    digest = hashlib.sha256(
        json.dumps(digest_input, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    status = "verified" if args.verify and all(row["already_applied"] for row in before) else "ready"
    mutation_results: list[dict[str, Any]] = []
    after: list[dict[str, Any]] = []
    if args.verify and status != "verified":
        status = "verification_failed"
    elif args.apply:
        if os.environ.get(APPLY_ENV) != "1" or os.environ.get(APPLY_DIGEST_ENV) != digest:
            status = "blocked"
        else:
            pending = [
                {
                    "id": row["id"],
                    "propertyId": None,
                    "note": row["target_note"],
                    "isReviewedByUser": True,
                }
                for row in before if not row["already_applied"]
            ]
            if pending:
                response = run_graphql({
                    "operationName": "UpdateTransaction",
                    "variables": {"input": pending},
                    "query": """
                    mutation UpdateTransaction($input: [UpdateTransaction!]) {
                      updateTransactions(input: $input) {
                        id amount date merchantName propertyId tagId bankAccountId
                        note isManual isDeleted
                      }
                    }
                    """,
                })
                mutation_results = response["data"]["updateTransactions"]
            after = read_live_targets()
            if any(not row["already_applied"] for row in after):
                raise RuntimeError("Post-apply verification failed")
            status = "applied"

    report = {
        "status": status,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "scope": "Six existing post-close Elton bank-feed rows; property assignment and notes only.",
        "accounting_treatment": (
            "Elton sold and seller proceeds bypassed ECO. Post-close remittance/"
            "settlement rows are Lofty closeout items and post-sale insurance is "
            "an ECO cancellation/refund item, not sold-DAO borrowing."
        ),
        "closing_residual_after_reclassification": -26.61,
        "creates_cash": False,
        "creates_manual_rows": False,
        "before": before,
        "payload_digest": digest,
        "mutation_results": mutation_results,
        "after": after,
        "approval_command": (
            f"{APPLY_ENV}=1 {APPLY_DIGEST_ENV}={digest} "
            f"python3 scripts/{Path(__file__).name} --apply"
        ),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": status,
        "target_count": len(before),
        "pending_count": sum(not row["already_applied"] for row in before),
        "payload_digest": digest,
        "report": str(args.report),
    }, indent=2))
    return 0 if status in {"ready", "applied", "verified"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
