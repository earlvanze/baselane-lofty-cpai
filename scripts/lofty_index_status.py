#!/usr/bin/env python3
from __future__ import annotations

import re

ACTIVE_INDEX_STATUSES = {"created", "existing", "would_create"}
EXCLUDED_INDEX_STATUSES = {"skipped_closed", "skipped_sold", "skipped_delisted", "sold", "delisted", "closed"}


def normalize_index_status(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    if not text:
        return ""
    tokens = {token for token in text.split("_") if token}
    if "delisted" in tokens:
        return "skipped_delisted" if text.startswith("skipped") else "delisted"
    if "sold" in tokens:
        return "skipped_sold" if text.startswith("skipped") else "sold"
    if "closed" in tokens:
        return "skipped_closed" if text.startswith("skipped") else "closed"
    return text


def is_active_index_status(value: object) -> bool:
    return normalize_index_status(value) in ACTIVE_INDEX_STATUSES


def is_excluded_index_status(value: object) -> bool:
    status = normalize_index_status(value)
    return status in EXCLUDED_INDEX_STATUSES or status.startswith("skipped_")
