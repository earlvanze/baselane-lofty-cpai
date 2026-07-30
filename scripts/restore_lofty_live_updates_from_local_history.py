#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).absolute().parents[1]
if not (ROOT / "skills" / "lofty-pm" / "scripts" / "publish_latest_update_to_lofty.py").is_file():
    ROOT = Path(__file__).resolve().parents[1]
PAGES_SCRIPTS = ROOT / "skills" / "lofty-pm" / "scripts"
MCP_SCRIPTS = ROOT / "skills" / "lofty-pm" / "scripts"
sys.path.insert(0, str(MCP_SCRIPTS))
sys.path.insert(0, str(PAGES_SCRIPTS))

PROPERTY_UPDATE_MARKER_RE = re.compile(r"(?mi)^\s*-\s+\*{0,2}\s*Property Update\s*\(")
ANY_HEADING_RE = re.compile(r"(?m)^#{1,6}\s+")
TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")
DEFAULT_OUTPUT_DIR = ROOT / "tmp" / "lofty-live-updates-full-local-restore"
DEFAULT_RUNTIME_MAPS = [
    ROOT / "skills" / "lofty-pm" / "config" / "property_update_map.json",
    ROOT / "reports" / "baselane_financials_monthly_lofty_pm_runtime_map.json",
    ROOT / "reports" / "lofty-pm-runtime-map.json",
]


def load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def property_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if not isinstance(data, dict):
        return []
    rows = data.get("records") or data.get("properties") or []
    return [row for row in rows if isinstance(row, dict)]


def manager_properties(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    rows = data.get("properties")
    if not isinstance(rows, list):
        rows = ((data.get("response") or {}).get("data") or {}).get("properties")
    return [row for row in (rows or []) if isinstance(row, dict)]


def summarize_updates(text: str) -> dict[str, Any]:
    return {
        "char_count": len(text),
        "line_count": len(text.splitlines()),
        "marker_count": len(PROPERTY_UPDATE_MARKER_RE.findall(text)),
        "heading_count": len(ANY_HEADING_RE.findall(text)),
        "table_line_count": sum(1 for line in text.splitlines() if TABLE_LINE_RE.match(line)),
        "has_property_updates_header": bool(re.search(r"(?mi)^#\s+Property Updates\s*$", text)),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def build_payloads(args: argparse.Namespace) -> list[dict[str, Any]]:
    containment = load_module(
        "lofty_live_updates_history_containment_report_restore",
        ROOT / "scripts" / "lofty_live_updates_history_containment_report.py",
    )
    publish = load_module(
        "publish_latest_update_to_lofty_restore",
        PAGES_SCRIPTS / "publish_latest_update_to_lofty.py",
    )
    runtime_records = containment.merge_runtime_map_records(args.runtime_map)
    live_ids: set[str] | None = None
    if args.manager_properties:
        live_rows = manager_properties(load_json(args.manager_properties))
        live_ids = {str(row.get("id") or row.get("lofty_property_id") or "").strip() for row in live_rows}
    manual_excluded = set(containment.DEFAULT_MANUAL_EXCLUDED_PROPERTIES)
    manual_excluded.update(args.manual_excluded_property or [])
    payloads: list[dict[str, Any]] = []
    for row in runtime_records:
        property_id = str(row.get("lofty_property_id") or row.get("property_id") or "").strip()
        property_name = str(row.get("property_name") or row.get("match_key") or property_id).strip()
        if not property_id:
            continue
        if live_ids is not None and property_id not in live_ids:
            continue
        if containment.property_excluded(property_name, manual_excluded):
            continue
        updates_md, updates_md_resolution = containment.resolve_updates_md(Path(str(row.get("updates_md") or "")))
        if not updates_md.is_file():
            continue
        source_text = updates_md.read_text(encoding="utf-8")
        source_marker_count = len(PROPERTY_UPDATE_MARKER_RE.findall(source_text))
        entries = publish.parse_entries(source_text)
        updates = publish.listing_lofty_text(entries, include_history=True)
        summary = summarize_updates(updates)
        if summary["marker_count"] != source_marker_count:
            raise SystemExit(
                f"Refusing payload with marker mismatch for {property_name}: "
                f"source_markers={source_marker_count} rendered_markers={summary['marker_count']}"
            )
        if summary["has_property_updates_header"]:
            raise SystemExit(f"Refusing payload with full UPDATES.md header for {property_name}")
        payloads.append(
            {
                "property_name": property_name,
                "lofty_property_id": property_id,
                "updates_md": str(updates_md),
                "updates_md_resolution": updates_md_resolution,
                "entry_count": len(entries),
                "updates": updates,
                "updates_sha256": summary["sha256"],
                "updates_summary": summary,
            }
        )
    return sorted(payloads, key=lambda row: (row["property_name"].lower(), row["lofty_property_id"]))


def write_payload_artifacts(payloads: list[dict[str, Any]], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload_file = output_dir / "publish_payloads.json"
    payload_file.write_text(json.dumps(payloads, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return payload_file


def apply_payloads(payloads: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    update_bridge = load_module(
        "update_lofty_pm_property_restore_apply",
        PAGES_SCRIPTS / "update_lofty_pm_property.py",
    )
    ctx = update_bridge.ensure_lofty_cdp_context(mode="list", close_extras=True)
    target_id = ctx["targetId"]
    bridge = update_bridge.install_turbopack_bridge(target_id)
    if bridge.get("ok") is not True:
        raise SystemExit(f"Lofty bridge failed: {bridge}")
    chunks = []
    for index in range(0, len(payloads), args.chunk_size):
        rows = payloads[index : index + args.chunk_size]
        patches = [{"id": row["lofty_property_id"], "updates": row["updates"]} for row in rows]
        expr = f"""(async () => {{
          const patches = {json.dumps(patches, ensure_ascii=False)};
          const bridge = globalThis.__openclawLoftyBridge;
          if (!bridge?.ok || typeof bridge.managerModifyProperty !== 'function') {{
            return {{ok: false, error: 'managerModifyProperty bridge missing'}};
          }}
          const results = [];
          for (const patch of patches) {{
            try {{
              const result = await bridge.managerModifyProperty(patch);
              results.push({{id: patch.id, ok: true, acceptedRuntimeReturn: result != null, resultType: typeof result}});
            }} catch (err) {{
              results.push({{id: patch.id, ok: false, error: String(err), stack: String(err && err.stack || '').slice(0, 500)}});
            }}
          }}
          return {{ok: results.every(r => r.ok), results}};
        }})()"""
        response = update_bridge.runtime_eval(target_id, expr, await_promise=True, timeout=args.timeout_seconds)
        value = response.get("result", {}).get("result", {}).get("value") or {}
        chunks.append(
            {
                "chunk_index": index // args.chunk_size + 1,
                "property_ids": [row["lofty_property_id"] for row in rows],
                "response": value,
                "cdp_exception": response.get("result", {}).get("exceptionDetails"),
            }
        )
        if value.get("ok") is not True:
            break
        time.sleep(args.chunk_pause_seconds)
    return {"target_id": target_id, "url": ctx.get("url"), "chunks": chunks}


def readback(payloads: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    update_bridge = load_module(
        "update_lofty_pm_property_restore_readback",
        PAGES_SCRIPTS / "update_lofty_pm_property.py",
    )
    containment = load_module(
        "lofty_live_updates_history_containment_report_restore_readback",
        ROOT / "scripts" / "lofty_live_updates_history_containment_report.py",
    )
    publish = containment.load_publish_module(PAGES_SCRIPTS)
    markdown = containment.load_markdown_module(PAGES_SCRIPTS)
    read_payload = load_json(args.read_payload)
    data, ctx = update_bridge.get_manager_properties_data_via_turbopack_bridge(read_payload, close_extra_tabs=True)
    props = data.get("data", {}).get("properties", []) if isinstance(data, dict) else []
    live_by_id = {prop.get("id"): prop for prop in props if isinstance(prop, dict)}
    rows = []
    for expected in payloads:
        live = live_by_id.get(expected["lofty_property_id"])
        live_updates = live.get("updates") if isinstance(live, dict) else ""
        live_updates = live_updates if isinstance(live_updates, str) else ""
        history_containment = containment.containment_for_updates(
            publish,
            markdown,
            Path(str(expected["updates_md"])),
            live_updates,
        )
        matches_expected = live_updates == expected["updates"]
        contains_expected_history = history_containment.get("containment_ok") is True
        rows.append(
            {
                "property_name": expected["property_name"],
                "lofty_property_id": expected["lofty_property_id"],
                "found_live": live is not None,
                "matches_expected": matches_expected,
                "contains_expected_history": contains_expected_history,
                "expected": expected["updates_summary"],
                "live": summarize_updates(live_updates),
                "history_containment": history_containment,
            }
        )
    return {
        "target_id": ctx.get("targetId"),
        "url": ctx.get("url"),
        "checked_count": len(rows),
        "matched_count": sum(1 for row in rows if row["matches_expected"]),
        "mismatch_count": sum(1 for row in rows if not row["matches_expected"]),
        "containment_match_count": sum(1 for row in rows if row["contains_expected_history"]),
        "containment_mismatch_count": sum(1 for row in rows if not row["contains_expected_history"]),
        "rows": rows,
    }


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    payloads = build_payloads(args)
    payload_file = write_payload_artifacts(payloads, args.output_dir)
    report = {
        "generated_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "mode": "restore_lofty_live_updates_from_local_history",
        "dry_run": args.dry_run,
        "sends_owner_email": False,
        "payload_file": str(payload_file),
        "property_count": len(payloads),
        "total_entry_count": sum(int(row["entry_count"]) for row in payloads),
        "payload_summaries": [
            {key: row[key] for key in ("property_name", "lofty_property_id", "updates_md", "entry_count", "updates_summary")}
            for row in payloads
        ],
    }
    if args.dry_run:
        report["status"] = "ok_dry_run"
        return report
    apply = apply_payloads(payloads, args)
    report["apply"] = apply
    chunk_failures = [chunk for chunk in apply["chunks"] if (chunk.get("response") or {}).get("ok") is not True]
    if chunk_failures:
        report["status"] = "failed"
        report["failed_count"] = len(chunk_failures)
        return report
    proof = readback(payloads, args)
    report["readback"] = proof
    report["status"] = "ok" if proof["containment_mismatch_count"] == 0 else "review"
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Restore Lofty live listing updates from guarded local UPDATES.md history.")
    parser.add_argument("--runtime-map", type=Path, action="append", default=list(DEFAULT_RUNTIME_MAPS))
    parser.add_argument("--manager-properties", type=Path, default=None)
    parser.add_argument("--read-payload", type=Path, default=ROOT / "tmp" / "lofty-pm-monthly-publish" / "manager.get-manager-properties.payload.json")
    parser.add_argument("--manual-excluded-property", action="append", default=[])
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--report", type=Path, default=ROOT / "reports" / "lofty_live_updates_full_local_restore.json")
    parser.add_argument("--chunk-size", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=int, default=240)
    parser.add_argument("--chunk-pause-seconds", type=float, default=1.0)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    report = build_report(args)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({key: report.get(key) for key in ("status", "dry_run", "property_count", "total_entry_count")}, indent=2))
    return 0 if report["status"] in {"ok", "ok_dry_run"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
