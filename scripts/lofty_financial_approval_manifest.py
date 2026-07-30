#!/usr/bin/env python3
"""Hash-bound approval records for canonical property FINANCIALS.md files."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write(path: Path, *, run_month: str, approvals: list[dict[str, Any]]) -> None:
    payload = {
        "generated_at": iso_z(),
        "run_month": run_month,
        "schema_version": 1,
        "approval_count": len(approvals),
        "approvals": approvals,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def approved_candidate(
    manifest: dict[str, Any], *, run_month: str, canonical_financials: Path
) -> dict[str, Any] | None:
    if str(manifest.get("run_month") or "") != run_month:
        return None
    canonical = str(canonical_financials)
    for record in manifest.get("approvals") or []:
        if not isinstance(record, dict):
            continue
        if record.get("canonical_financials") == canonical and record.get("approved") is True:
            candidate = Path(str(record.get("candidate_path") or ""))
            digest = str(record.get("candidate_sha256") or "")
            if candidate.is_file() and digest and sha256_file(candidate) == digest:
                return record
    return None
