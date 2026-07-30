#!/usr/bin/env python3
"""Build a non-mutating rollback plan for Aligned owner-statement imports.

The live importer writes created-row manifests. This helper reads those
manifests and produces the transaction IDs, idempotency keys, and settlement
relabel restoration data needed for an operator-reviewed rollback.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
DEFAULT_MANIFEST_DIR = ROOT / "reports" / "aligned-owner-statement-import-manifests"
DEFAULT_REPORT = ROOT / "reports" / "aligned_owner_statement_rollback_plan.json"
DEFAULT_CSV = ROOT / "reports" / "aligned_owner_statement_rollback_plan.csv"


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"_read_error": str(exc)}
    return payload if isinstance(payload, dict) else {"rows": payload}


def manifest_paths(manifest_dir: Path | None, explicit_paths: list[Path]) -> list[Path]:
    paths: list[Path] = []
    if manifest_dir and manifest_dir.is_dir():
        paths.extend(sorted(manifest_dir.glob("*.json")))
    paths.extend(explicit_paths)
    deduped: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        try:
            key = path.resolve()
        except Exception:
            key = path
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


def build_plan(paths: list[Path]) -> dict[str, Any]:
    created_rows: list[dict[str, Any]] = []
    settlement_rows: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    seen_created_ids: set[str] = set()
    seen_created_keys: set[str] = set()
    seen_settlement_ids: set[str] = set()

    for path in paths:
        payload = read_json(path)
        source = {
            "path": str(path),
            "exists": path.is_file(),
            "status": "error" if payload.get("_read_error") else "ok",
            "error": payload.get("_read_error"),
            "batch": payload.get("batch"),
            "month": payload.get("month"),
            "created_count": payload.get("created_count"),
            "settlement_relabel_updated_count": payload.get("settlement_relabel_updated_count"),
        }
        rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
        source["row_count"] = len(rows)
        for row in rows:
            if not isinstance(row, dict):
                continue
            tx_id = str(row.get("baselane_id") or "").strip()
            key = str(row.get("idempotency_key") or "").strip()
            unique_key = tx_id or key
            if not unique_key:
                continue
            if tx_id and tx_id in seen_created_ids:
                continue
            if not tx_id and key in seen_created_keys:
                continue
            if tx_id:
                seen_created_ids.add(tx_id)
            if key:
                seen_created_keys.add(key)
            created_rows.append(
                {
                    "source_manifest": str(path),
                    "batch": payload.get("batch"),
                    "month": payload.get("month"),
                    "baselane_id": tx_id,
                    "idempotency_key": key,
                    "propertyId": row.get("propertyId"),
                    "date": row.get("date"),
                    "amount": row.get("amount"),
                    "merchantName": row.get("merchantName"),
                    "note": row.get("note"),
                    "rollback_action": "delete_or_hide_created_manual_transaction",
                }
            )

        for row in payload.get("settlement_relabel_rollback_rows") or []:
            if not isinstance(row, dict):
                continue
            tx_id = str(row.get("id") or "").strip()
            if not tx_id or tx_id in seen_settlement_ids:
                continue
            seen_settlement_ids.add(tx_id)
            settlement_rows.append(
                {
                    "source_manifest": str(path),
                    "batch": payload.get("batch"),
                    "month": payload.get("month"),
                    "id": tx_id,
                    "propertyId": row.get("propertyId"),
                    "date": row.get("date"),
                    "amount": row.get("amount"),
                    "merchantName": row.get("merchantName"),
                    "oldTagId": row.get("oldTagId"),
                    "oldNote": row.get("oldNote"),
                    "newTagId": row.get("newTagId"),
                    "newNote": row.get("newNote"),
                    "rollback_action": "restore_settlement_tag_and_note",
                }
            )
        sources.append(source)

    missing_created_ids = [row for row in created_rows if not row.get("baselane_id")]
    return {
        "job": "baselane-aligned-owner-statement-rollback-plan",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "review" if missing_created_ids else "ok",
        "apply": False,
        "manifest_sources": sources,
        "manifest_count": len(paths),
        "created_transaction_count": len(created_rows),
        "created_transaction_ids": [row["baselane_id"] for row in created_rows if row.get("baselane_id")],
        "missing_created_id_count": len(missing_created_ids),
        "settlement_restore_count": len(settlement_rows),
        "operator_steps": [
            "Review this plan against the live Baselane Transactions UI or an authenticated query.",
            "Delete or hide only created manual transactions whose IDs and key= notes match this plan.",
            "Restore settlement rows to oldTagId and oldNote only when settlement_restore_count is nonzero.",
            "Refresh the Baselane ledger export and rerun validate_aligned_owner_statement_downstream.py after rollback.",
        ],
        "created_transactions": created_rows,
        "settlement_relabel_restores": settlement_rows,
    }


def write_csv(path: Path, plan: dict[str, Any]) -> None:
    rows = []
    for row in plan.get("created_transactions") or []:
        out = dict(row)
        out["record_type"] = "created_transaction"
        rows.append(out)
    for row in plan.get("settlement_relabel_restores") or []:
        out = dict(row)
        out["record_type"] = "settlement_restore"
        rows.append(out)
    if not rows:
        return
    fields = sorted({key for row in rows for key in row})
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build rollback plan from Aligned owner-statement created manifests")
    parser.add_argument("--manifest-dir", type=Path, default=DEFAULT_MANIFEST_DIR)
    parser.add_argument("--manifest", type=Path, action="append", default=[])
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--no-csv", action="store_true")
    args = parser.parse_args()

    paths = manifest_paths(args.manifest_dir, args.manifest)
    plan = build_plan(paths)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if not args.no_csv:
        write_csv(args.csv, plan)
    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0 if plan["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
