"""Stable identity for the monthly transfer reconciliation report."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def _stable_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _stable_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if not str(key).endswith("_generated_at")
            and str(key) not in {"generated_at", "sent_at"}
            and not str(key).endswith("_digest")
            and not str(key).endswith("_sha256")
        }
    if isinstance(value, list):
        return [_stable_value(item) for item in value]
    return value


def stable_transfer_report_digest(path: Path) -> str | None:
    """Hash financial/reporting content, excluding volatile timestamps and digests."""
    if path is None or not path.is_file():
        return None
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if not any(
        str(key).endswith("_generated_at")
        or str(key) in {"generated_at", "sent_at"}
        or str(key).endswith("_digest")
        or str(key).endswith("_sha256")
        for key in payload
    ):
        return hashlib.sha256(raw).hexdigest()
    canonical = json.dumps(_stable_value(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
