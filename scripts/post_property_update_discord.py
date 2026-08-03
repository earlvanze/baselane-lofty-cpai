#!/usr/bin/env python3
"""Resolve Lofty property Discord routes from the canonical property map.

This compatibility surface is intentionally read-only. It replaces a removed
workspace-local helper so guarded publication does not depend on an untracked
OpenClaw script.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from discord_summary_routing_policy import LOFTY_GUILD_ID, LOFTY_GUILD_NAME


ROOT = Path(__file__).resolve().parents[1]
PROPERTY_MAP = ROOT / "skills" / "lofty-pm" / "config" / "property_update_map.json"
DESTINATION_PURPOSE = "lofty_property_financial_summary"


def normalized_address(value: object) -> str:
    text = str(value or "").split(",", 1)[0].casefold().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _properties() -> list[dict[str, Any]]:
    payload = json.loads(PROPERTY_MAP.read_text(encoding="utf-8"))
    records = payload.get("properties") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise RuntimeError(f"invalid Lofty property route map: {PROPERTY_MAP}")
    return [record for record in records if isinstance(record, dict)]


def channel_for_property(property_name: object) -> tuple[str, bool]:
    """Return a unique mapped channel; ambiguity and missing maps fail closed."""
    needle = normalized_address(property_name)
    if not needle:
        return "", False
    matches: set[str] = set()
    for record in _properties():
        candidates = (
            record.get("property_name"),
            record.get("full_address"),
            record.get("slug"),
        )
        if any(normalized_address(candidate) == needle for candidate in candidates):
            channel = str(record.get("discord_channel_id") or "").strip()
            if channel.isdigit():
                matches.add(channel)
    return (next(iter(matches)), True) if len(matches) == 1 else ("", False)


def shared_target_allowed(target: object, property_names: list[str]) -> bool:
    """Allow a shared channel only when every property resolves to that channel."""
    channel = str(target or "").removeprefix("channel:").strip()
    if not channel or len(property_names) < 2:
        return False
    resolved = [channel_for_property(name) for name in property_names]
    return all(matched and mapped == channel for mapped, matched in resolved)
