#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from baselane_cleanup_non1456_aligned_import import (
    ROOT,
    iso_z,
    note_text,
    query_property_transactions,
    query_transactions,
    update_transactions,
)


REPORT = ROOT / "reports" / "baselane_evernest_note_cleanup.json"
TARGET_PROPERTY_ID = "81428"
ALIGNED_KEY_PREFIXES = (
    "aligned-1456-w-85th-st-cleveland-oh-4410",
    "aligned-1456w85",
)
KEY_RE = re.compile(r"(?:^|\s)key=([^|\s]+)")


def key_from_note(note: str) -> str:
    match = KEY_RE.search(note or "")
    return match.group(1) if match else ""


def clean_note(note: str) -> str:
    cleaned = note.replace("Aligned/Evernest clearing detail import", "Aligned clearing detail import")
    cleaned = cleaned.replace("Evernest/Aligned clearing detail import", "Aligned clearing detail import")
    cleaned = cleaned.replace("Evernest clearing detail import", "Aligned clearing detail import")
    cleaned = re.sub(r"\bEvernest\b", "Aligned", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def by_id(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        tx_id = str(row.get("id") or "")
        if tx_id:
            out[tx_id] = row
    return list(out.values())


def looks_like_guarded_aligned_import(row: dict[str, Any]) -> bool:
    note = note_text(row.get("note"))
    key = key_from_note(note)
    return (
        bool(row.get("isManual"))
        and str(row.get("propertyId") or "") == TARGET_PROPERTY_ID
        and any(key.startswith(prefix) for prefix in ALIGNED_KEY_PREFIXES)
        and "clearing detail import" in note.lower()
        and "accounting/manual detail only, no eco bank transfer" in note.lower()
    )


def fetch_candidate_rows() -> list[dict[str, Any]]:
    return by_id(query_transactions("Evernest") + query_transactions("Aligned/Evernest"))


def property_rows_with_evernest_notes() -> list[dict[str, Any]]:
    return [
        row
        for row in query_property_transactions(TARGET_PROPERTY_ID)
        if "evernest" in note_text(row.get("note")).lower()
    ]


def evernest_search_rows() -> list[dict[str, Any]]:
    return query_transactions("Evernest")


def row_summary(row: dict[str, Any]) -> dict[str, Any]:
    note = note_text(row.get("note"))
    return {
        "id": str(row.get("id") or ""),
        "date": row.get("date"),
        "amount": row.get("amount"),
        "merchantName": row.get("merchantName"),
        "propertyId": row.get("propertyId"),
        "isManual": row.get("isManual"),
        "key": key_from_note(note),
        "note": note,
    }


def stable_digest(updates: list[dict[str, Any]], deletes: list[dict[str, Any]]) -> str:
    payload = {
        "target_property_id": TARGET_PROPERTY_ID,
        "note_update_ids": sorted(str(item["id"]) for item in updates),
        "delete_ids": sorted(str(item["id"]) for item in deletes),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Remove Evernest from guarded live Baselane Aligned-import notes.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args()

    candidates = fetch_candidate_rows()
    note_updates: list[dict[str, Any]] = []
    evernest_source_deletes: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []

    for row in candidates:
        note = note_text(row.get("note"))
        merchant = str(row.get("merchantName") or "")
        note_has_evernest = "evernest" in note.lower()
        merchant_has_evernest = "evernest" in merchant.lower()
        guarded = looks_like_guarded_aligned_import(row)

        if not (note_has_evernest or merchant_has_evernest):
            continue
        if not guarded:
            issues.append({"issue": "unguarded_evernest_match", "row": row_summary(row)})
            continue
        if merchant_has_evernest:
            evernest_source_deletes.append(row)
            continue
        if note_has_evernest:
            cleaned = clean_note(note)
            if "evernest" in cleaned.lower():
                issues.append({"issue": "note_cleanup_failed", "row": row_summary(row), "cleaned_note": cleaned})
            elif cleaned != note:
                note_updates.append({**row, "cleaned_note": cleaned})

    digest = stable_digest(note_updates, evernest_source_deletes)
    status = "blocked" if issues else "ready"
    mutation_results: dict[str, Any] = {}
    post_evernest_rows: list[dict[str, Any]] = []
    post_property_note_rows: list[dict[str, Any]] = []

    if args.apply and not issues:
        update_inputs = [
            {"id": str(row["id"]), "note": row["cleaned_note"], "isReviewedByUser": True}
            for row in note_updates
        ]
        delete_inputs = [
            {
                "id": str(row["id"]),
                "note": clean_note(note_text(row.get("note"))),
                "isDeleted": True,
                "isReviewedByUser": True,
            }
            for row in evernest_source_deletes
        ]
        mutation_results["note_updates"] = update_transactions(update_inputs)
        mutation_results["evernest_source_deletes"] = update_transactions(delete_inputs)
        post_evernest_rows = evernest_search_rows()
        post_property_note_rows = property_rows_with_evernest_notes()
        status = "applied" if not post_evernest_rows and not post_property_note_rows else "post_verify_failed"

    report = {
        "job": "baselane-remove-evernest-from-aligned-notes",
        "generated_at": iso_z(),
        "status": status,
        "apply": bool(args.apply),
        "target_property_id": TARGET_PROPERTY_ID,
        "candidate_count": len(candidates),
        "note_update_count": len(note_updates),
        "evernest_source_delete_count": len(evernest_source_deletes),
        "payload_digest": digest,
        "issues": issues,
        "note_update_targets": [
            {**row_summary(row), "cleaned_note": row["cleaned_note"]}
            for row in note_updates
        ],
        "evernest_source_delete_targets": [row_summary(row) for row in evernest_source_deletes],
        "mutation_results": mutation_results,
        "post_evernest_search_count": len(post_evernest_rows),
        "post_evernest_search_rows": [row_summary(row) for row in post_evernest_rows],
        "post_property_evernest_note_count": len(post_property_note_rows),
        "post_property_evernest_note_rows": [row_summary(row) for row in post_property_note_rows],
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": report["status"],
        "candidate_count": report["candidate_count"],
        "note_update_count": report["note_update_count"],
        "evernest_source_delete_count": report["evernest_source_delete_count"],
        "payload_digest": report["payload_digest"],
        "post_evernest_search_count": report["post_evernest_search_count"],
        "post_property_evernest_note_count": report["post_property_evernest_note_count"],
    }, indent=2))
    return 0 if status in {"ready", "applied"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
