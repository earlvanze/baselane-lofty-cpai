#!/usr/bin/env python3
"""Safely add a generated disabled mortgage downloader entry to the registry."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from stable_json_report import write_json_report

SCRIPT_PATH = Path(__file__).absolute()
WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", SCRIPT_PATH.parents[1]))
DEFAULT_CONFIG = WORKSPACE_ROOT / "config" / "mortgage_statement_downloaders.json"

ALLOWED_KEYS = {
    "id",
    "enabled",
    "property",
    "servicer",
    "co_owner_paid_mortgage",
    "env",
    "runtime",
    "script",
    "report",
    "notes",
    "portal_url",
}
ALLOWED_RUNTIMES = {"node", "python", "bash"}
FORBIDDEN_TEXT_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"authorization",
        r"bearer",
        r"cookie",
        r"password",
        r"secret",
        r"token",
    ]
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_property(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def load_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, str(exc)
    if not isinstance(data, dict):
        return None, "JSON root is not an object"
    return data, None


def contains_forbidden_text(value: object) -> bool:
    if isinstance(value, dict):
        return any(contains_forbidden_text(key) or contains_forbidden_text(item) for key, item in value.items())
    if isinstance(value, list):
        return any(contains_forbidden_text(item) for item in value)
    text = str(value or "")
    return any(pattern.search(text) for pattern in FORBIDDEN_TEXT_PATTERNS)


def validation_errors(entry: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    unknown_keys = sorted(set(entry) - ALLOWED_KEYS)
    if unknown_keys:
        errors.append(f"unknown_entry_keys:{','.join(unknown_keys)}")
    for key in ["id", "property", "runtime", "script", "report"]:
        if not str(entry.get(key) or "").strip():
            errors.append(f"missing_{key}")
    if entry.get("enabled") is not False:
        errors.append("registry_entry_must_be_disabled")
    if entry.get("co_owner_paid_mortgage") is not True:
        errors.append("co_owner_paid_mortgage_must_be_true")
    if str(entry.get("runtime") or "") not in ALLOWED_RUNTIMES:
        errors.append("unsupported_runtime")
    sensitive_scan = {key: value for key, value in entry.items() if key != "notes"}
    if contains_forbidden_text(sensitive_scan):
        errors.append("possible_secret_or_auth_material_detected")
    return errors


def load_config(path: Path) -> tuple[dict[str, Any], str | None]:
    if not path.exists():
        return {"version": 1, "downloaders": []}, None
    data, error = load_json(path)
    if error or data is None:
        return {}, error or "config_unreadable"
    if not isinstance(data.get("downloaders"), list):
        return {}, "downloaders is not a list"
    return data, None


def same_entry(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return {key: left.get(key) for key in ALLOWED_KEYS if key in left} == {
        key: right.get(key) for key in ALLOWED_KEYS if key in right
    }


def build_report(
    entry_path: Path,
    config_path: Path,
    *,
    apply: bool,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "job": "register-mortgage-downloader-registry-entry",
        "generated_at": utc_now(),
        "entry_path": str(entry_path),
        "entry_exists": entry_path.exists(),
        "config_path": str(config_path),
        "config_exists": config_path.exists(),
        "apply": apply,
        "safe_to_run_automatically": True,
        "safe_to_register_automatically": False,
        "status": "review",
        "reason": None,
        "config_written": False,
    }

    entry, entry_error = load_json(entry_path)
    if entry_error or entry is None:
        report.update(reason="registry_entry_unreadable", error=entry_error)
        return report

    report.update(
        {
            "entry_id": entry.get("id"),
            "property": entry.get("property"),
            "servicer": entry.get("servicer"),
            "entry_enabled": entry.get("enabled"),
        }
    )
    errors = validation_errors(entry)
    if errors:
        report.update(reason="registry_entry_invalid", validation_errors=errors)
        return report

    config, config_error = load_config(config_path)
    if config_error:
        report.update(reason="config_unreadable", error=config_error)
        return report

    entries = config.get("downloaders")
    if not isinstance(entries, list):
        report.update(reason="downloaders is not a list")
        return report
    report["entries_before"] = len(entries)
    entry_id = str(entry.get("id") or "")
    prop_key = normalize_property(entry.get("property"))
    duplicate = next((item for item in entries if isinstance(item, dict) and item.get("id") == entry_id), None)
    if isinstance(duplicate, dict):
        if same_entry(duplicate, entry):
            report.update(
                {
                    "status": "ok",
                    "reason": "already_present",
                    "entries_after": len(entries),
                    "would_write": False,
                }
            )
            return report
        report.update(
            reason="duplicate_id_conflict",
            duplicate_id=entry_id,
            conflicting_entry_id=duplicate.get("id"),
            conflicting_entry_enabled=duplicate.get("enabled"),
            conflicting_entry_runtime=duplicate.get("runtime"),
            conflicting_entry_script=duplicate.get("script"),
            conflicting_entry_report=duplicate.get("report"),
            conflicting_entry_property=duplicate.get("property"),
        )
        return report

    property_conflict = next(
        (
            item
            for item in entries
            if isinstance(item, dict) and normalize_property(item.get("property")) == prop_key
        ),
        None,
    )
    if isinstance(property_conflict, dict):
        report.update(
            reason="property_already_configured",
            conflicting_entry_id=property_conflict.get("id"),
            conflicting_entry_enabled=property_conflict.get("enabled"),
            conflicting_entry_runtime=property_conflict.get("runtime"),
            conflicting_entry_script=property_conflict.get("script"),
            conflicting_entry_report=property_conflict.get("report"),
        )
        return report

    report.update(
        {
            "status": "ok",
            "reason": "ready_to_append_disabled_entry" if not apply else None,
            "would_write": not apply,
            "entries_after": len(entries) + 1,
        }
    )
    if not apply:
        return report

    updated = {**config, "downloaders": [*entries, entry]}
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
    report.update(config_written=True, would_write=False)
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entry", required=True, type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, stdout: TextIO = sys.stdout) -> int:
    args = parse_args(argv)
    report = build_report(args.entry, args.config, apply=args.apply)
    if args.report:
        report = write_json_report(args.report, report)
    print(json.dumps(report, indent=2, sort_keys=True), file=stdout)
    return 0 if report.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
