#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from lofty_monthly_exclusions import DEFAULT_MANUAL_EXCLUDED_PROPERTIES
except Exception:  # noqa: BLE001
    DEFAULT_MANUAL_EXCLUDED_PROPERTIES = (
        "3560 Saint Albans Rd",
        "1935 S Glen Rd",
        "402 N Wild Olive Ave",
        "9919 S Oglesby",
    )

PROPERTY_UPDATE_MARKER_RE = re.compile(r"(?m)^-\s+\*{0,2}Property Update\s*\(")
DEFAULT_RUNTIME_MAPS = [
    Path("skills/lofty-pm/config/property_update_map.json"),
    Path("reports/baselane_financials_monthly_lofty_pm_runtime_map.json"),
    Path("reports/lofty-pm-runtime-map.json"),
]
DEFAULT_MANAGER_PROPERTIES_CANDIDATES = [
    "reports/lofty_manager_properties_refresh_*.json",
    "reports/baselane_financials_monthly_live_update_capture*.json",
    "reports/lofty-pm-current/get-manager-properties.full-response.json",
    "reports/lofty_88_manager_properties_refresh_after_loan_zero.json",
]

def iso_z() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def normalize(value: str) -> str:
    text = value.lower()
    text = re.sub(r"\blfty\d+\b", " ", text)
    text = text.replace("&", "and")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\bavenue\b", "ave", text)
    text = re.sub(r"\bstreet\b", "st", text)
    text = re.sub(r"\blane\b", "ln", text)
    text = re.sub(r"\bnorth\b", "n", text)
    text = re.sub(r"\bohio\b", "oh", text)
    return re.sub(r"\s+", " ", text).strip()


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_publish_module(skill_scripts_dir: Path) -> Any:
    path = skill_scripts_dir / "publish_latest_update_to_lofty.py"
    spec = importlib.util.spec_from_file_location("lofty_publish_latest_update_history_containment", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load publish module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(skill_scripts_dir))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def load_markdown_module(skill_scripts_dir: Path) -> Any:
    path = skill_scripts_dir / "lofty_update_markdown.py"
    spec = importlib.util.spec_from_file_location("lofty_update_markdown_history_containment", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load markdown module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(skill_scripts_dir))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def normalize_update_text_for_containment(markdown_module: Any, text: str) -> str:
    normalized = markdown_module.fix_mojibake(text or "")
    normalized = markdown_module.flatten_markdown_tables(normalized)
    normalized = re.sub(
        r"(?mi)^\s*-\s+\*{0,2}\s*Property Update\s*\((\d{2}/\d{2}/\d{4})\):\*{0,2}\s*",
        r"- **Property Update (\1):**\n",
        normalized,
    )
    normalized = re.sub(r"(?mi)^\s*#\s+Property Updates\s*$\n?", "", normalized)
    normalized = re.sub(r"(?mi)^\s*##\s+\d{4}-\d{2}-\d{2}\s*$\n?", "", normalized)
    lines: list[str] = []
    in_update = False
    for raw_line in normalized.splitlines():
        line = raw_line.rstrip()
        if re.match(r"^- \*\*Property Update \(\d{2}/\d{2}/\d{4}\):\*\*$", line):
            in_update = True
            lines.append(line)
        elif in_update and line.strip():
            child = re.sub(r"^\s*-\s*", "", line).strip()
            lines.append(f"    - {child}")
        elif not line.strip():
            lines.append("")
        else:
            lines.append(line)
    normalized = "\n".join(lines)
    normalized = re.sub(r"[ \t]+$", "", normalized, flags=re.M)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def load_records(path: Path) -> list[dict[str, Any]]:
    data = load_json(path)
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        rows = data.get("records") or data.get("properties") or []
        return [row for row in rows if isinstance(row, dict)]
    return []


def merge_runtime_map_records(paths: list[Path]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for path in paths:
        if not path.is_file():
            continue
        for record in load_records(path):
            updates_md = str(record.get("updates_md") or "").strip()
            property_id = str(record.get("lofty_property_id") or record.get("property_id") or "").strip()
            if not updates_md or not property_id:
                continue
            key = property_id or updates_md
            current = merged.get(key, {})
            merged[key] = {
                **current,
                **record,
                "map_source": current.get("map_source") or str(path),
            }
    return sorted(
        merged.values(),
        key=lambda row: (
            normalize(str(row.get("property_name") or row.get("match_key") or row.get("updates_md") or "")),
            str(row.get("lofty_property_id") or ""),
        ),
    )


def manager_properties(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    data = load_json(path)
    rows = data.get("properties") if isinstance(data, dict) else None
    if not isinstance(rows, list) and isinstance(data, dict):
        rows = ((data.get("data") or {}).get("properties") if isinstance(data.get("data"), dict) else None)
    if not isinstance(rows, list) and isinstance(data, dict):
        response = data.get("response") if isinstance(data.get("response"), dict) else data
        payload = response.get("data") if isinstance(response.get("data"), dict) else response
        rows = payload.get("properties") if isinstance(payload, dict) else None
    return [row for row in (rows or []) if isinstance(row, dict)]


def select_default_manager_properties(patterns: list[str] | None = None) -> Path:
    candidates: list[Path] = []
    for pattern in patterns or DEFAULT_MANAGER_PROPERTIES_CANDIDATES:
        candidates.extend(Path().glob(pattern))
    full_captures: list[Path] = []
    for path in candidates:
        if not path.is_file():
            continue
        try:
            properties = manager_properties(path)
        except Exception:
            continue
        if len(properties) >= 50:
            full_captures.append(path)
    if not full_captures:
        return Path("reports/lofty-pm-current/get-manager-properties.full-response.json")
    return max(full_captures, key=lambda path: path.stat().st_mtime)


def mapped_by_property_id(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("lofty_property_id") or row.get("property_id") or "").strip(): row
        for row in records
        if str(row.get("lofty_property_id") or row.get("property_id") or "").strip()
    }


def load_guard_snapshots(artifacts_root: Path) -> dict[str, dict[str, Any]]:
    snapshots: dict[str, dict[str, Any]] = {}
    if not artifacts_root.is_dir():
        return snapshots
    for manifest_path in artifacts_root.glob("*/manifest.json"):
        try:
            manifest = load_json(manifest_path)
        except Exception:
            continue
        target_path = str(manifest.get("target_path") or "").strip()
        snapshot_path = Path(str(manifest.get("live_snapshot_path") or ""))
        if not target_path or not snapshot_path.is_file():
            continue
        snapshots[target_path] = {
            "manifest_path": str(manifest_path),
            "live_snapshot_path": str(snapshot_path),
            "fetched_at": manifest.get("fetched_at"),
            "registered_at": manifest.get("registered_at"),
            "compare_mode": manifest.get("compare_mode"),
            "live_text": snapshot_path.read_text(encoding="utf-8", errors="replace"),
        }
    return snapshots


def property_excluded(name: str, manual_excluded: set[str]) -> bool:
    normalized = normalize(name)
    return any(normalize(excluded) in normalized for excluded in manual_excluded if excluded)


def resolve_updates_md(path: Path) -> tuple[Path, str]:
    if path.is_file():
        return path, "input"
    parts = path.parts
    if "Public" not in parts:
        return path, "missing"
    try:
        public_index = parts.index("Public")
    except ValueError:
        return path, "missing"
    if public_index < 1:
        return path, "missing"
    property_root = Path(*parts[:public_index])
    suffix = Path(*parts[public_index + 1 :])
    target_key = normalize(property_root.name)
    candidates: list[tuple[int, Path]] = []
    if property_root.parent.is_dir():
        for candidate in property_root.parent.iterdir():
            if not candidate.is_dir() or not candidate.name.endswith(" Public"):
                continue
            candidate_key = normalize(candidate.name.removesuffix(" Public"))
            if not candidate_key:
                continue
            score = 0
            if candidate_key == target_key:
                score = 1000 + len(candidate_key)
            elif candidate_key in target_key or target_key.startswith(candidate_key + " "):
                score = 500 + len(candidate_key)
            if not score:
                continue
            candidate_path = candidate / suffix
            if candidate_path.is_file():
                candidates.append((score, candidate_path))
    if not candidates:
        return path, "missing"
    candidates.sort(key=lambda item: (item[0], str(item[1])), reverse=True)
    return candidates[0][1], "public_sibling"


def containment_for_updates(module: Any, markdown_module: Any, updates_md: Path, live_text: str) -> dict[str, Any]:
    try:
        entries = module.parse_entries(updates_md.read_text(encoding="utf-8"))
    except (Exception, SystemExit) as exc:  # noqa: BLE001
        return {
            "containment_ok": False,
            "parse_error": f"{type(exc).__name__}: {exc}",
            "local_entry_count": 0,
            "live_marker_count": len(PROPERTY_UPDATE_MARKER_RE.findall(live_text or "")),
            "missing_entry_count": 0,
            "missing_entries": [],
        }

    live = (live_text or "").strip()
    normalized_live = normalize_update_text_for_containment(markdown_module, live)
    missing_entries: list[dict[str, Any]] = []
    for index, entry in enumerate(entries, start=1):
        expected = str(module.entry_lofty_text(entry) or "").strip()
        normalized_expected = normalize_update_text_for_containment(markdown_module, expected)
        if expected and expected not in live and normalized_expected not in normalized_live:
            missing_entries.append(
                {
                    "date": entry.get("date"),
                    "index": index,
                    "char_count": len(expected),
                    "sha256": digest_text(expected),
                    "preview": expected[:240],
                }
            )

    return {
        "containment_ok": not missing_entries,
        "parse_error": None,
        "local_entry_count": len(entries),
        "live_marker_count": len(PROPERTY_UPDATE_MARKER_RE.findall(live)),
        "missing_entry_count": len(missing_entries),
        "missing_entries": missing_entries[:10],
    }


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    module = load_publish_module(args.skill_scripts_dir)
    markdown_module = load_markdown_module(args.skill_scripts_dir)
    guard_snapshots = load_guard_snapshots(args.guard_artifacts_root)
    manual_excluded = set(DEFAULT_MANUAL_EXCLUDED_PROPERTIES)
    manual_excluded.update(args.manual_excluded_property or [])

    runtime_records = merge_runtime_map_records(args.runtime_map)
    by_id = mapped_by_property_id(runtime_records)
    live_properties = manager_properties(args.manager_properties)

    records: list[dict[str, Any]] = []
    unmapped_live_properties: list[dict[str, Any]] = []
    for live in live_properties:
        property_id = str(live.get("id") or live.get("lofty_property_id") or "").strip()
        live_name = str(live.get("address") or live.get("assetName") or property_id).strip()
        mapped = by_id.get(property_id)
        live_text = str(live.get("updates") or "")
        if not mapped:
            unmapped_live_properties.append(
                {
                    "id": property_id,
                    "assetName": live.get("assetName"),
                    "address": live.get("address"),
                    "assetUnit": live.get("assetUnit"),
                    "STATUS": live.get("STATUS"),
                    "updates_marker_count": len(PROPERTY_UPDATE_MARKER_RE.findall(live_text)),
                    "updates_length": len(live_text),
                    "excluded": property_excluded(live_name, manual_excluded),
                }
            )
            continue

        updates_md = Path(str(mapped.get("updates_md") or ""))
        resolved_updates_md, updates_md_resolution = resolve_updates_md(updates_md)
        guard_snapshot = guard_snapshots.get(str(resolved_updates_md)) or guard_snapshots.get(str(updates_md))
        effective_live_text = str(live_text or "")
        live_updates_source = "manager_properties"
        if not effective_live_text and guard_snapshot:
            effective_live_text = str(guard_snapshot.get("live_text") or "")
            live_updates_source = "guard_snapshot"
        base = {
            "property_name": mapped.get("property_name") or mapped.get("match_key") or live_name,
            "lofty_property_id": property_id,
            "assetUnit": live.get("assetUnit"),
            "updates_md": str(updates_md),
            "resolved_updates_md": str(resolved_updates_md),
            "updates_md_resolution": updates_md_resolution,
            "map_source": mapped.get("map_source"),
            "updates_md_exists": resolved_updates_md.is_file(),
            "excluded": property_excluded(str(mapped.get("property_name") or live_name), manual_excluded),
            "live_updates_length": len(effective_live_text),
            "live_updates_source": live_updates_source,
            "live_snapshot_path": guard_snapshot.get("live_snapshot_path") if guard_snapshot else None,
            "live_snapshot_fetched_at": guard_snapshot.get("fetched_at") if guard_snapshot else None,
        }
        if not resolved_updates_md.is_file():
            records.append(
                {
                    **base,
                    "containment_ok": False,
                    "parse_error": None,
                    "local_entry_count": 0,
                    "live_marker_count": len(PROPERTY_UPDATE_MARKER_RE.findall(effective_live_text)),
                    "missing_entry_count": 0,
                    "missing_entries": [],
                }
            )
            continue
        records.append({**base, **containment_for_updates(module, markdown_module, resolved_updates_md, effective_live_text)})

    if not live_properties:
        for mapped in runtime_records:
            updates_md = Path(str(mapped.get("updates_md") or ""))
            records.append(
                {
                    "property_name": mapped.get("property_name") or mapped.get("match_key"),
                    "lofty_property_id": mapped.get("lofty_property_id"),
                    "updates_md": str(updates_md),
                    "map_source": mapped.get("map_source"),
                    "updates_md_exists": updates_md.is_file(),
                    "excluded": property_excluded(str(mapped.get("property_name") or ""), manual_excluded),
                    "containment_ok": False,
                    "parse_error": None,
                    "local_entry_count": 0,
                    "live_marker_count": 0,
                    "missing_entry_count": 0,
                    "missing_entries": [],
                    "live_updates_length": 0,
                }
            )

    nonexcluded = [record for record in records if record.get("excluded") is not True]
    missing_records = [record for record in nonexcluded if int(record.get("missing_entry_count") or 0) > 0]
    parse_error_records = [record for record in nonexcluded if record.get("parse_error")]
    missing_updates_md_records = [record for record in nonexcluded if record.get("updates_md_exists") is not True]
    unmapped_nonexcluded = [record for record in unmapped_live_properties if record.get("excluded") is not True]
    summary = {
        "generated_at": iso_z(),
        "live_properties": len(live_properties),
        "mapped_live_properties": len(records) if live_properties else 0,
        "runtime_map_properties": len(runtime_records),
        "guard_snapshot_property_count": len(guard_snapshots),
        "guard_snapshot_used_count": sum(1 for record in records if record.get("live_updates_source") == "guard_snapshot"),
        "unmapped_live_property_count": len(unmapped_live_properties),
        "unmapped_nonexcluded_live_property_count": len(unmapped_nonexcluded),
        "containment_ok_count": sum(1 for record in records if record.get("containment_ok") is True),
        "missing_entry_property_count": len(missing_records),
        "missing_entry_count": sum(int(record.get("missing_entry_count") or 0) for record in missing_records),
        "missing_updates_md_count": len(missing_updates_md_records),
        "parse_error_count": len(parse_error_records),
        "all_known_nonexcluded_guarded_live_histories_containment_ok": (
            not missing_records and not parse_error_records and not missing_updates_md_records
        ),
    }
    status = "ok" if summary["all_known_nonexcluded_guarded_live_histories_containment_ok"] else "review"
    return {
        "status": status,
        "summary": summary,
        "records": records,
        "unmapped_live_properties": unmapped_live_properties,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify live Lofty updates fields still contain local UPDATES.md history.")
    parser.add_argument("--runtime-map", type=Path, action="append", default=[])
    parser.add_argument("--manager-properties", type=Path)
    parser.add_argument(
        "--guard-artifacts-root",
        type=Path,
        default=Path("/home/digit/.openclaw/workspace-lofty-vp/.openclaw/lofty-updates-fetch"),
    )
    parser.add_argument("--skill-scripts-dir", type=Path, default=Path("skills/lofty-pm/scripts"))
    parser.add_argument("--manual-excluded-property", action="append", default=[])
    parser.add_argument("--report", type=Path, default=Path("reports/lofty_live_updates_history_containment_report.json"))
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not args.runtime_map:
        args.runtime_map = DEFAULT_RUNTIME_MAPS
    if args.manager_properties is None:
        args.manager_properties = select_default_manager_properties()
    report = build_report(args)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], **report["summary"]}, indent=2))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
