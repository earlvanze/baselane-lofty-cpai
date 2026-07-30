#!/usr/bin/env python3
"""Apply Alcott tax/insurance accrual corrections to live Baselane.

This is intentionally narrow: it only handles 326-332 S Alcott tax/insurance
accrual markers from 2025-07 through 2026-07.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
GRAPHQL_HELPER = ROOT / "scripts" / "baselane_graphql_via_cdp.js"
DEFAULT_GL = Path("/home/digit/Dropbox/Projects/assetrail/ECO Systems General Ledger.csv")
DEFAULT_REPORT = ROOT / "reports" / "alcott_tax_insurance_live_apply_20260711.json"

PROPERTY_TEMPLATE = "326-332 S Alcott St, Denver, CO 80219"
PROPERTY_BASELANE = "326 South Alcott Street"
PROPERTY_ID = "77356"
TAG_IDS = {
    "insurance": "65",
    "taxes": "95",
}
MONTHS = [f"{year}-{month:02d}" for year, month in [
    (2025, 7),
    (2025, 8),
    (2025, 9),
    (2025, 10),
    (2025, 11),
    (2025, 12),
    (2026, 1),
    (2026, 2),
    (2026, 3),
    (2026, 4),
    (2026, 5),
    (2026, 6),
    (2026, 7),
]]
MARKER_RE = re.compile(
    r"(AOPS-PAU-ACCRUAL)\|(?P<kind>taxes|insurance)\|"
    + re.escape(PROPERTY_TEMPLATE)
    + r"\|(?P<month>\d{4}-\d{2})\|(?P<amount>\d+(?:\.\d+)?)"
)


def iso_z() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def month_end_iso(month: str) -> str:
    year, month_number = [int(part) for part in month.split("-")]
    if month_number == 12:
        last = dt.date(year, 12, 31)
    else:
        last = dt.date(year, month_number + 1, 1) - dt.timedelta(days=1)
    return last.isoformat()


def note_text(note: Any) -> str:
    if isinstance(note, dict):
        return str(note.get("text") or "")
    return str(note or "")


def parse_marker(notes: str) -> dict[str, str] | None:
    match = MARKER_RE.search(notes or "")
    if not match:
        return None
    return {
        "kind": match.group("kind"),
        "month": match.group("month"),
        "amount": match.group("amount"),
        "prefix": notes[: match.end("month")],
    }


def read_targets(gl_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with gl_path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            marker = parse_marker(str(row.get("Notes") or ""))
            if not marker:
                continue
            if marker["month"] not in MONTHS:
                continue
            if str(row.get("Property") or "") != PROPERTY_BASELANE:
                continue
            amount = round(float(str(row.get("Amount") or "0").replace(",", "")), 2)
            if amount >= 0:
                continue
            rows.append({
                "kind": marker["kind"],
                "month": marker["month"],
                "marker_prefix": f"AOPS-PAU-ACCRUAL|{marker['kind']}|{PROPERTY_TEMPLATE}|{marker['month']}",
                "merchantName": row.get("Merchant") or row.get("Description") or f"{marker['kind'].title()} Accrual | {PROPERTY_TEMPLATE} | {marker['month']}",
                "note": row.get("Notes") or "",
                "tagId": TAG_IDS[marker["kind"]],
                "propertyId": PROPERTY_ID,
                "unitId": None,
                "entityId": None,
                "date": month_end_iso(marker["month"]),
                "bankAccountId": None,
                "amount": amount,
                "isReviewedByUser": True,
            })
    rows.sort(key=lambda item: (item["month"], item["kind"]))
    return rows


def run_graphql(payload: dict[str, Any]) -> dict[str, Any]:
    if not GRAPHQL_HELPER.exists():
        raise FileNotFoundError(f"missing GraphQL helper: {GRAPHQL_HELPER}")
    helper_timeout_ms = int(os.environ.get("BASELANE_GQL_TIMEOUT_MS") or "90000")
    command_timeout_ms = int(os.environ.get("BASELANE_GQL_COMMAND_TIMEOUT_MS") or "15000")
    helper_timeout_seconds = max(30, (helper_timeout_ms + (2 * command_timeout_ms) + 10000 + 999) // 1000)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle)
        payload_path = handle.name
    try:
        proc = subprocess.run(
            ["node", str(GRAPHQL_HELPER), payload_path],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=os.environ.copy(),
            timeout=helper_timeout_seconds,
        )
    finally:
        Path(payload_path).unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError("\n".join(part for part in [proc.stderr.strip(), proc.stdout.strip()] if part) or f"GraphQL helper rc={proc.returncode}")
    data = json.loads(proc.stdout)
    if data.get("errors"):
        raise RuntimeError(json.dumps(data["errors"], indent=2))
    return data


def query_transactions(search: str, page_limit: int = 500) -> list[dict[str, Any]]:
    payload = {
        "operationName": "Transactions",
        "variables": {
            "input": {
                "sort": {"direction": "DESC", "field": "date"},
                "filter": {"search": search, "isHidden": False, "isDeleted": False},
                "page": 1,
                "pageLimit": page_limit,
            }
        },
        "query": """
        query Transactions($input: SortsAndFilters) {
          transactions(input: $input) {
            total
            data {
              id
              amount
              date
              merchantName
              bankAccountId
              propertyId
              tagId
              note
              isManual
              hidden
              isDeleted
            }
          }
        }
        """,
    }
    result = run_graphql(payload)["data"]["transactions"]
    return result.get("data") or []


def query_target_transactions() -> list[dict[str, Any]]:
    """Fetch all likely live Alcott AOPS rows with one Baselane lookup."""
    rows = query_transactions(f"AOPS-PAU-ACCRUAL|{PROPERTY_TEMPLATE}")
    if rows:
        return rows
    # Baselane search can be tokenized differently across UI/API versions.
    # Fall back to a broader marker search, then filter locally.
    return query_transactions("AOPS-PAU-ACCRUAL")


def create_transaction(target: dict[str, Any]) -> dict[str, Any]:
    variables = {
        key: target[key]
        for key in ["merchantName", "note", "tagId", "propertyId", "unitId", "entityId", "date", "bankAccountId", "amount", "isReviewedByUser"]
    }
    payload = {
        "operationName": "createTransaction",
        "variables": variables,
        "query": """
        mutation createTransaction($merchantName: String!, $note: String!, $tagId: ID, $propertyId: ID, $unitId: ID, $entityId: Int, $date: String!, $bankAccountId: ID, $amount: Float!, $isReviewedByUser: Boolean) {
          createTransaction(input: { merchantName: $merchantName note: $note tagId: $tagId propertyId: $propertyId unitId: $unitId entityId: $entityId date: $date bankAccountId: $bankAccountId amount: $amount isReviewedByUser: $isReviewedByUser }) {
            id
            merchantName
            bankAccountId
            amount
            isManual
            tagId
            propertyId
            date
            note
          }
        }
        """,
    }
    return run_graphql(payload)["data"]["createTransaction"]


def update_transaction(target: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "operationName": "UpdateTransaction",
        "variables": {
            "input": [{
                "id": str(live["id"]),
                "amount": float(target["amount"]),
                "note": target["note"],
                "tagId": target["tagId"],
                "propertyId": target["propertyId"],
                "unitId": None,
            }]
        },
        "query": """
        mutation UpdateTransaction($input: [UpdateTransaction!]) {
          updateTransactions(input: $input) {
            id
            note
            propertyId
            tagId
            amount
          }
        }
        """,
    }
    return run_graphql(payload)["data"]["updateTransactions"][0]


def live_matches(target: dict[str, Any], live: dict[str, Any]) -> bool:
    live_note = note_text(live.get("note"))
    live_amount = round(float(live.get("amount") or 0), 2)
    return (
        live_amount == round(float(target["amount"]), 2)
        and str(live.get("propertyId") or "") == PROPERTY_ID
        and str(live.get("tagId") or "") == target["tagId"]
        and target["note"] == live_note
    )


def select_live_match(target: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    prefix = target["marker_prefix"]
    matches = [
        row for row in rows
        if prefix in note_text(row.get("note"))
        and str(row.get("propertyId") or "") == PROPERTY_ID
        and str(row.get("tagId") or "") == target["tagId"]
    ]
    if not matches:
        return None
    matches.sort(key=lambda row: (not bool(row.get("isManual")), str(row.get("id") or "")))
    return matches[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply Alcott tax/insurance accruals to live Baselane.")
    parser.add_argument("--gl-csv", type=Path, default=DEFAULT_GL)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--apply", action="store_true", help="Create/update live Baselane manual transactions")
    args = parser.parse_args(argv)

    targets = read_targets(args.gl_csv)
    if len(targets) != 26:
        raise RuntimeError(f"expected 26 Alcott tax/insurance targets, found {len(targets)}")

    actions = []
    created = []
    updated = []
    skipped = []
    live_rows = query_target_transactions()
    for target in targets:
        live = select_live_match(target, live_rows)
        if live is None:
            action = {"action": "create", "target": target}
            if args.apply:
                action["result"] = create_transaction(target)
                created.append(action["result"])
            actions.append(action)
            continue
        if live_matches(target, live):
            skipped.append({"reason": "already_current", "id": live.get("id"), "target": target})
            continue
        action = {"action": "update", "id": live.get("id"), "target": target, "live_amount": live.get("amount"), "live_note": note_text(live.get("note"))}
        if args.apply:
            action["result"] = update_transaction(target, live)
            updated.append(action["result"])
        actions.append(action)

    report = {
        "generated_at": iso_z(),
        "mode": "apply" if args.apply else "dry_run",
        "gl_csv": str(args.gl_csv),
        "target_count": len(targets),
        "action_count": len(actions),
        "create_count": sum(1 for action in actions if action["action"] == "create"),
        "update_count": sum(1 for action in actions if action["action"] == "update"),
        "skipped_count": len(skipped),
        "created_count": len(created),
        "updated_count": len(updated),
        "actions": actions,
        "skipped": skipped,
        "status": "ok",
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": report["status"],
        "mode": report["mode"],
        "targets": report["target_count"],
        "actions": report["action_count"],
        "creates": report["create_count"],
        "updates": report["update_count"],
        "skipped": report["skipped_count"],
        "created": report["created_count"],
        "updated": report["updated_count"],
        "report": str(args.report),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
