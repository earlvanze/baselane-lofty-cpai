from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any, Iterable


DEFAULT_VOLATILE_FIELDS = frozenset({"generated_at", "idempotency_digest"})


def without_volatile_report_fields(
    value: Any,
    *,
    volatile_fields: Iterable[str] = DEFAULT_VOLATILE_FIELDS,
) -> Any:
    excluded = frozenset(volatile_fields)
    if isinstance(value, dict):
        return {
            key: without_volatile_report_fields(item, volatile_fields=excluded)
            for key, item in value.items()
            if key not in excluded
        }
    if isinstance(value, list):
        return [
            without_volatile_report_fields(item, volatile_fields=excluded)
            for item in value
        ]
    return value


def stable_report_digest(
    value: Any,
    *,
    volatile_fields: Iterable[str] = DEFAULT_VOLATILE_FIELDS,
) -> str:
    stable_value = without_volatile_report_fields(
        value, volatile_fields=volatile_fields
    )
    payload = json.dumps(stable_value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _restore_volatile_fields(
    current: Any,
    previous: Any,
    volatile_fields: frozenset[str],
) -> Any:
    if isinstance(current, dict) and isinstance(previous, dict):
        restored = dict(current)
        for key, value in current.items():
            if key in volatile_fields and key in previous:
                restored[key] = previous[key]
            elif key in previous:
                restored[key] = _restore_volatile_fields(
                    value, previous[key], volatile_fields
                )
        return restored
    if (
        isinstance(current, list)
        and isinstance(previous, list)
        and len(current) == len(previous)
    ):
        return [
            _restore_volatile_fields(item, previous[index], volatile_fields)
            for index, item in enumerate(current)
        ]
    return current


def write_json_report(
    path: Path,
    report: dict[str, Any],
    *,
    volatile_fields: Iterable[str] = DEFAULT_VOLATILE_FIELDS,
) -> dict[str, Any]:
    excluded = frozenset(volatile_fields)
    stable_report = dict(report)
    stable_report["idempotency_digest"] = stable_report_digest(
        stable_report, volatile_fields=excluded
    )
    if path.is_file():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = None
        if (
            isinstance(previous, dict)
            and without_volatile_report_fields(
                previous, volatile_fields=excluded
            )
            == without_volatile_report_fields(
                stable_report, volatile_fields=excluded
            )
        ):
            stable_report = _restore_volatile_fields(
                stable_report, previous, excluded
            )
    content = json.dumps(stable_report, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file() or path.read_text(encoding="utf-8") != content:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(content)
        temp_path.replace(path)
    return stable_report
