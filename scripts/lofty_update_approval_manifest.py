#!/usr/bin/env python3
"""Hash-bound approval records for canonical property UPDATES.md files."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from lofty_financial_approval_manifest import load, sha256_file, write


def approved_candidate(manifest: dict[str, Any], *, run_month: str, canonical_updates: Path) -> dict[str, Any] | None:
    if str(manifest.get("run_month") or "") != run_month:
        return None
    canonical = str(canonical_updates)
    for record in manifest.get("approvals") or []:
        if not isinstance(record, dict):
            continue
        if record.get("canonical_updates") != canonical or record.get("approved") is not True:
            continue
        candidate = Path(str(record.get("candidate_path") or ""))
        digest = str(record.get("candidate_sha256") or "")
        if candidate.is_file() and digest and sha256_file(candidate) == digest:
            return record
    return None

__all__ = ["approved_candidate", "load", "sha256_file", "write"]
