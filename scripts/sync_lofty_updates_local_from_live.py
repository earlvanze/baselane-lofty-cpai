#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONTAINMENT_SCRIPT = ROOT / "scripts" / "lofty_live_updates_history_containment_report.py"
DEFAULT_GUARD = Path("/home/digit/.openclaw/workspace-lofty-vp/scripts/lofty-updates-guard.py")


def load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def payload_property_ids(paths: list[Path]) -> set[str]:
    property_ids: set[str] = set()
    for path in paths:
        data = load_json(path)
        rows = (
            data
            if isinstance(data, list)
            else data.get("payload_summaries") or data.get("records") or []
        )
        for row in rows:
            if not isinstance(row, dict):
                continue
            property_id = str(row.get("lofty_property_id") or row.get("property_id") or "").strip()
            if property_id:
                property_ids.add(property_id)
    return property_ids


def render_local_history(guard: Any, live_text: str) -> tuple[str, list[str]]:
    chunks = guard.property_update_chunks(live_text)
    if not chunks:
        raise RuntimeError("live updates field has no parseable Property Update entries")
    sections: list[str] = ["# Property Updates"]
    dates: list[str] = []
    for chunk in chunks:
        update_date = guard.property_update_date(chunk)
        if not update_date:
            raise RuntimeError(f"live Property Update entry has no parseable date: {chunk[:160]!r}")
        dates.append(update_date)
        sections.append(f"## {update_date}\n\n{chunk.strip()}")
    rendered = "\n\n".join(sections).rstrip() + "\n"
    source_hashes = [guard.containment_chunk_sha256(chunk) for chunk in chunks]
    rendered_chunks = guard.property_update_chunks(rendered)
    rendered_hashes = [guard.containment_chunk_sha256(chunk) for chunk in rendered_chunks]
    if rendered_hashes != source_hashes:
        raise RuntimeError(
            "local rendering did not preserve live Property Update sequence "
            f"(live={len(source_hashes)} rendered={len(rendered_hashes)})"
        )
    return rendered, dates


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replace scoped local UPDATES.md files with canonical history fetched from live Lofty."
    )
    parser.add_argument("--runtime-map", type=Path, required=True)
    parser.add_argument("--manager-properties", type=Path, required=True)
    parser.add_argument("--payload-file", type=Path, action="append", required=True)
    parser.add_argument("--guard-script", type=Path, default=DEFAULT_GUARD)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    containment = load_module("lofty_updates_local_sync_containment", CONTAINMENT_SCRIPT)
    guard = load_module("lofty_updates_local_sync_guard", args.guard_script)
    target_ids = payload_property_ids(args.payload_file)
    if not target_ids:
        raise SystemExit("No target property IDs found in --payload-file inputs")

    records = containment.merge_runtime_map_records([args.runtime_map])
    records_by_id = {
        str(row.get("lofty_property_id") or row.get("property_id") or "").strip(): row
        for row in records
    }
    live_rows = containment.manager_properties(args.manager_properties)
    live_by_id = {str(row.get("id") or "").strip(): row for row in live_rows}

    missing_map = sorted(target_ids - records_by_id.keys())
    missing_live = sorted(target_ids - live_by_id.keys())
    if missing_map or missing_live:
        raise SystemExit(
            f"Refusing incomplete live-to-local sync: missing_map={missing_map} missing_live={missing_live}"
        )

    rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for property_id in sorted(target_ids):
        record = records_by_id[property_id]
        property_name = str(record.get("property_name") or record.get("match_key") or property_id)
        updates_md, resolution = containment.resolve_updates_md(Path(str(record.get("updates_md") or "")))
        live_text = str(live_by_id[property_id].get("updates") or "")
        try:
            rendered, dates = render_local_history(guard, live_text)
            before_text = updates_md.read_text(encoding="utf-8", errors="replace") if updates_md.is_file() else ""
            changed = before_text != rendered
            if args.apply and changed:
                atomic_write(updates_md, rendered)
            after_text = (
                updates_md.read_text(encoding="utf-8", errors="replace")
                if args.apply
                else rendered
            )
            if after_text != rendered:
                raise RuntimeError("post-write readback differs from prepared canonical local history")
            rows.append(
                {
                    "property_name": property_name,
                    "lofty_property_id": property_id,
                    "updates_md": str(updates_md),
                    "updates_md_resolution": resolution,
                    "entry_count": len(dates),
                    "latest_date": dates[0],
                    "oldest_date": dates[-1],
                    "changed": changed,
                    "applied": bool(args.apply and changed),
                    "before_sha256": sha256_text(before_text),
                    "after_sha256": sha256_text(after_text),
                    "live_updates_sha256": sha256_text(live_text),
                }
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(
                {
                    "property_name": property_name,
                    "lofty_property_id": property_id,
                    "updates_md": str(updates_md),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": "apply" if args.apply else "preview",
        "authoritative_source": str(args.manager_properties),
        "runtime_map": str(args.runtime_map),
        "payload_files": [str(path) for path in args.payload_file],
        "target_count": len(target_ids),
        "success_count": len(rows),
        "failure_count": len(failures),
        "changed_count": sum(1 for row in rows if row["changed"]),
        "applied_count": sum(1 for row in rows if row["applied"]),
        "properties": rows,
        "failures": failures,
        "status": "ok" if len(rows) == len(target_ids) and not failures else "review",
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
