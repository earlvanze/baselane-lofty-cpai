#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from baselane_apply_alcott_accruals_live import run_graphql as cdp_run_graphql


def workspace_root() -> Path:
    for candidate in (
        os.environ.get("WORKSPACE_ROOT"),
        "/home/digit/.openclaw/workspace",
        "/home/umbrel/.openclaw/workspace",
        str(Path(__file__).resolve().parents[1]),
    ):
        if candidate and Path(candidate).is_dir():
            return Path(candidate)
    return Path(__file__).resolve().parents[1]


ROOT = workspace_root()
MANIFEST_DIR = ROOT / "reports" / "aligned-owner-statement-import-manifests"
REPORT = ROOT / "reports" / "baselane_non1456_aligned_import_cleanup.json"
ALLOWED_PREFIX = "aligned-1456-w-85th-st-cleveland-oh-4410"
TARGET_PROPERTY_ID = "81779"
DEFAULT_HEADERS_PATH = ROOT / "reports" / "baselane_graphql_headers.json"


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def note_text(value: Any) -> str:
    return str(value.get("text") or "") if isinstance(value, dict) else str(value or "")


def saved_graphql_headers() -> dict[str, str] | None:
    configured = os.environ.get("BASELANE_GRAPHQL_HEADERS_JSON")
    path = Path(configured) if configured else DEFAULT_HEADERS_PATH
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    headers = payload.get("headers") if isinstance(payload, dict) else None
    if not isinstance(headers, dict):
        return None
    out = {str(key): str(value) for key, value in headers.items() if isinstance(value, str) and value}
    if not out.get("cookie") or not out.get("x-firebase-appcheck"):
        return None
    out["accept-encoding"] = "identity"
    return out


def run_graphql(payload: dict[str, Any]) -> dict[str, Any]:
    headers = saved_graphql_headers()
    if headers:
        request = urllib.request.Request(
            "https://orchestration.baselane.com/graphql",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:500]
            if exc.code == 401 and "APP_CHECK_TOKEN_INVALID" in body:
                return cdp_run_graphql(payload)
            raise RuntimeError(f"saved-header GraphQL HTTP {exc.code}: {body}") from exc
        if data.get("errors"):
            raise RuntimeError(json.dumps(data["errors"], indent=2))
        return data
    return cdp_run_graphql(payload)


def read_manifests(manifest_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    created: dict[str, dict[str, Any]] = {}
    settlement_restores: dict[str, dict[str, Any]] = {}
    for path in sorted(manifest_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        batch = str(payload.get("batch") or path.stem)
        month = payload.get("month")
        updated_settlements = int(payload.get("settlement_relabel_updated_count") or 0)
        for row in payload.get("rows") or []:
            if not isinstance(row, dict):
                continue
            key = str(row.get("idempotency_key") or "")
            tx_id = str(row.get("baselane_id") or "")
            if not key.startswith("aligned-") or key.startswith(ALLOWED_PREFIX) or not tx_id:
                continue
            created[tx_id] = {**row, "source_manifest": str(path), "batch": batch, "month": month}
        if updated_settlements <= 0:
            continue
        for row in payload.get("settlement_relabel_rollback_rows") or []:
            if not isinstance(row, dict):
                continue
            tx_id = str(row.get("id") or "")
            if not tx_id:
                continue
            settlement_restores[tx_id] = {**row, "source_manifest": str(path), "batch": batch, "month": month}
    return list(created.values()), list(settlement_restores.values())


def query_transactions(search: str, *, include_deleted: bool = False, page_limit: int = 500) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = {
            "operationName": "Transactions",
            "variables": {
                "input": {
                    "sort": {"direction": "DESC", "field": "date"},
                    "filter": {"search": search, "isHidden": False, "isDeleted": include_deleted},
                    "page": page,
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
        batch = result.get("data") or []
        rows.extend(batch)
        total = int(result.get("total") or 0)
        if not batch or len(rows) >= total or len(batch) < page_limit:
            return rows
        page += 1


def query_property_transactions(property_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = {
            "operationName": "Transactions",
            "variables": {
                "input": {
                    "sort": {"direction": "DESC", "field": "date"},
                    "filter": {"propertyId": property_id, "isHidden": False, "isDeleted": False},
                    "page": page,
                    "pageLimit": 1000,
                }
            },
            "query": """
            query Transactions($input: SortsAndFilters) {
              transactions(input: $input) {
                total
                data { id amount date merchantName propertyId tagId note isManual hidden isDeleted }
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


def verify_created_targets(manifest_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    verified: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for expected in manifest_rows:
        key = str(expected.get("idempotency_key") or "")
        tx_id = str(expected.get("baselane_id") or "")
        matches = [row for row in query_transactions(key) if str(row.get("id") or "") == tx_id]
        if not matches:
            issues.append({"id": tx_id, "key": key, "issue": "active_live_row_missing"})
            continue
        row = matches[0]
        actual = {
            "id": str(row.get("id") or ""),
            "propertyId": str(row.get("propertyId") or ""),
            "amount": round(float(row.get("amount") or 0), 2),
            "isManual": bool(row.get("isManual")),
            "isDeleted": bool(row.get("isDeleted")),
            "note": note_text(row.get("note")),
        }
        wanted_amount = round(float(expected.get("amount") or 0), 2)
        if actual["propertyId"] != TARGET_PROPERTY_ID:
            issues.append({"id": tx_id, "key": key, "issue": "property_mismatch", "actual": actual})
            continue
        if actual["amount"] != wanted_amount or actual["isDeleted"] or not actual["isManual"]:
            issues.append({"id": tx_id, "key": key, "issue": "identity_mismatch", "actual": actual})
            continue
        if key not in actual["note"] or "Aligned/Evernest clearing detail import" not in actual["note"]:
            issues.append({"id": tx_id, "key": key, "issue": "note_guard_failed", "actual": actual})
            continue
        verified.append({"manifest": expected, "live": row})
    return verified, issues


def verify_settlement_restores(restores: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows_by_id = {str(row.get("id") or ""): row for row in query_property_transactions(TARGET_PROPERTY_ID)}
    verified: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for expected in restores:
        tx_id = str(expected.get("id") or "")
        row = rows_by_id.get(tx_id)
        if not row:
            issues.append({"id": tx_id, "issue": "settlement_live_row_missing"})
            continue
        actual_note = note_text(row.get("note"))
        new_note = str(expected.get("newNote") or "")
        actual = {
            "id": tx_id,
            "propertyId": str(row.get("propertyId") or ""),
            "tagId": str(row.get("tagId") or ""),
            "amount": round(float(row.get("amount") or 0), 2),
            "date": str(row.get("date") or ""),
            "note": actual_note,
            "isDeleted": bool(row.get("isDeleted")),
        }
        wanted = {
            "propertyId": TARGET_PROPERTY_ID,
            "tagId": str(expected.get("newTagId") or ""),
            "amount": round(float(expected.get("amount") or 0), 2),
            "date": str(expected.get("date") or ""),
        }
        if any(actual[name] != wanted[name] for name in wanted):
            issues.append({"id": tx_id, "issue": "settlement_identity_mismatch", "actual": actual, "wanted": wanted})
            continue
        if new_note and actual_note != new_note:
            issues.append({"id": tx_id, "issue": "settlement_note_mismatch", "actual": actual, "wanted_note": new_note})
            continue
        verified.append({"manifest": expected, "live": row})
    return verified, issues


def stable_digest(created: list[dict[str, Any]], settlements: list[dict[str, Any]]) -> str:
    payload = {
        "delete_ids": sorted(str(item["live"]["id"]) for item in created),
        "restore_ids": sorted(str(item["live"]["id"]) for item in settlements),
        "allowed_prefix": ALLOWED_PREFIX,
        "property_id": TARGET_PROPERTY_ID,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def update_transactions(inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not inputs:
        return []
    payload = {
        "operationName": "UpdateTransaction",
        "variables": {"input": inputs},
        "query": """
        mutation UpdateTransaction($input: [UpdateTransaction!]) {
          updateTransactions(input: $input) {
            id
            amount
            date
            merchantName
            propertyId
            tagId
            note
            isDeleted
          }
        }
        """,
    }
    return run_graphql(payload)["data"]["updateTransactions"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Delete mistaken non-1456 Aligned/Evernest Baselane import rows.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args()

    manifest_created, manifest_settlements = read_manifests(MANIFEST_DIR)
    verified_created, created_issues = verify_created_targets(manifest_created)
    verified_settlements, settlement_issues = verify_settlement_restores(manifest_settlements)
    digest = stable_digest(verified_created, verified_settlements)
    status = "ready"
    mutation_results: dict[str, Any] = {}
    post_active_rows: list[dict[str, Any]] = []

    if created_issues or settlement_issues:
        status = "blocked"
    elif args.apply:
        delete_inputs = [
            {"id": str(item["live"]["id"]), "isDeleted": True, "isReviewedByUser": True}
            for item in verified_created
        ]
        restore_inputs = [
            {
                "id": str(item["live"]["id"]),
                "tagId": str(item["manifest"].get("oldTagId") or ""),
                "note": str(item["manifest"].get("oldNote") or ""),
                "isReviewedByUser": True,
            }
            for item in verified_settlements
        ]
        mutation_results["deleted_rows"] = update_transactions(delete_inputs)
        mutation_results["restored_settlements"] = update_transactions(restore_inputs)
        post_active_rows = query_transactions("aligned-8708-willard-ave-cleveland-oh-44")
        if post_active_rows:
            status = "post_verify_failed"
        else:
            status = "applied"

    report = {
        "job": "baselane-non1456-aligned-import-cleanup",
        "generated_at": iso_z(),
        "status": status,
        "apply": bool(args.apply),
        "allowed_aligned_prefix": ALLOWED_PREFIX,
        "target_property_id": TARGET_PROPERTY_ID,
        "manifest_created_count": len(manifest_created),
        "verified_delete_count": len(verified_created),
        "verified_delete_amount_total": round(sum(float(item["manifest"].get("amount") or 0) for item in verified_created), 2),
        "verified_settlement_restore_count": len(verified_settlements),
        "payload_digest": digest,
        "created_issues": created_issues,
        "settlement_issues": settlement_issues,
        "delete_targets": [
            {
                "id": str(item["live"]["id"]),
                "key": str(item["manifest"].get("idempotency_key") or ""),
                "date": item["live"].get("date"),
                "amount": item["live"].get("amount"),
                "propertyId": item["live"].get("propertyId"),
                "merchantName": item["live"].get("merchantName"),
            }
            for item in verified_created
        ],
        "settlement_restore_targets": [
            {
                "id": str(item["live"]["id"]),
                "date": item["live"].get("date"),
                "amount": item["live"].get("amount"),
                "oldTagId": item["manifest"].get("oldTagId"),
                "newTagId": item["manifest"].get("newTagId"),
            }
            for item in verified_settlements
        ],
        "mutation_results": mutation_results,
        "post_active_non1456_aligned_count": len(post_active_rows),
        "post_active_non1456_aligned_rows": post_active_rows,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("status", "verified_delete_count", "verified_delete_amount_total", "verified_settlement_restore_count", "payload_digest", "post_active_non1456_aligned_count")}, indent=2))
    return 0 if status in {"ready", "applied"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
