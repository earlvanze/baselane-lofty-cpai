#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lofty_index_status import is_active_index_status, is_excluded_index_status, normalize_index_status
from lofty_monthly_exclusions import (
    DEFAULT_MANUAL_EXCLUDED_PROPERTIES,
    append_unmapped_exclusion_records,
    financial_hold_exclusion_records,
    guarded_apply_exclusion_records,
    match_exclusion_guard,
    monthly_exclusion_guards,
)
from lofty_live_updates_history_containment_report import (
    load_markdown_module,
    normalize_update_text_for_containment,
)
from lofty_property_paths import display_name_for_property_path, public_dir_for_property, resolve_index_property_path

UPDATES_DIR_NAME = "00 - README & Property Snapshot"
GUARD_TIMEOUT_SECONDS = 30
LOFTY_CDP_RECOVERY_ACTION = (
    "Hard-refresh or close/open Lofty property-owners tab; authenticate only if still redirected, then rerun live UPDATES.md capture."
)
LOFTY_VISIBLE_AUTH_ACTION = "Auth Lofty visible tab (3 tries); then rerun live UPDATES.md capture."
SAFE_MONTHLY_CRON_DRY_RUN_COMMAND = (
    "DRY_RUN=1 CAPTURE_LOFTY_LIVE_GUARDS_IN_DRY_RUN=1 "
    "SEND_OWNER_EMAILS=0 PUBLISH_LOFTY_PM_UPDATES=0 "
    "RUN_LOFTY_GUARDED_APPLY=1 APPLY_LOFTY_GUARDED_UPDATES=0 bash scripts/baselane_financials_monthly_cron.sh"
)

def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


def property_id_from_href(value: str) -> str:
    match = re.search(r"/property-owners/edit/([A-Z0-9]+)", value or "")
    return match.group(1) if match else ""


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def stable_digest(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def load_current_only_verify(root: Path, target_count: int) -> dict[str, dict[str, Any]]:
    return {}


def publish_state_current_only_verify(updates_md: Path, property_id: str, live_text: str, run_month: str) -> dict[str, Any] | None:
    return None


def load_publish_module(skill_scripts_dir: Path) -> Any:
    path = skill_scripts_dir / "publish_latest_update_to_lofty.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("lofty_publish_latest_update_for_live_guard", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def full_history_containment(updates_md: Path, live_text: str, skill_scripts_dir: Path) -> dict[str, Any]:
    module = load_publish_module(skill_scripts_dir)
    if module is None:
        return {
            "ok": False,
            "status": "missing_publish_renderer",
            "missing_entry_count": 0,
            "entry_count": 0,
            "missing_entries": [],
        }
    markdown_module = load_markdown_module(skill_scripts_dir)
    try:
        entries = module.parse_entries(updates_md.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "status": "parse_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "missing_entry_count": 0,
            "entry_count": 0,
            "missing_entries": [],
        }
    live = (live_text or "").strip()
    normalized_live = normalize_update_text_for_containment(markdown_module, live)
    expected_texts: list[dict[str, Any]] = []
    missing_entries: list[dict[str, Any]] = []
    for entry in entries:
        expected = str(module.entry_lofty_text(entry) or "").strip()
        normalized_expected = normalize_update_text_for_containment(markdown_module, expected)
        digest = hashlib.sha256(normalized_expected.encode("utf-8")).hexdigest() if normalized_expected else ""
        row = {
            "date": entry.get("date"),
            "char_count": len(expected),
            "sha256": digest,
        }
        expected_texts.append(row)
        if normalized_expected and normalized_expected not in normalized_live:
            missing_entries.append(row)
    return {
        "ok": not missing_entries,
        "status": "ok" if not missing_entries else "missing_history_entries",
        "entry_count": len(expected_texts),
        "missing_entry_count": len(missing_entries),
        "missing_entries": missing_entries[:10],
        "expected_marker_count": len(getattr(module, "PROPERTY_UPDATE_MARKER_RE").findall("\n\n".join(str(module.entry_lofty_text(entry) or "").strip() for entry in entries))),
        "live_marker_count": len(getattr(module, "PROPERTY_UPDATE_MARKER_RE").findall(live)),
    }


def load_index(index_csv: Path) -> list[dict[str, str]]:
    with index_csv.open(newline="", encoding="utf-8") as handle:
        return [row for row in csv.DictReader(handle)]


def filter_rows_by_property(rows: list[dict[str, str]], property_names: list[str]) -> list[dict[str, str]]:
    wanted = {normalize(value) for value in property_names if normalize(value)}
    if not wanted:
        return rows
    selected: list[dict[str, str]] = []
    for row in rows:
        property_path, path_resolution = resolve_index_property_path(row)
        candidates = {
            normalize(property_path.name),
            normalize(display_name_for_property_path(property_path, path_resolution)),
            normalize(str(row.get("managed_name") or "")),
        }
        if wanted & candidates:
            selected.append(row)
    return selected


def property_id_candidates(portfolio_map: Path | None, skill_map: Path | None) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    if portfolio_map and portfolio_map.is_file():
        data = load_json(portfolio_map)
        rows = data.get("properties") if isinstance(data, dict) else data
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            property_id = str(row.get("lofty_property_id") or row.get("property_id") or row.get("propertyId") or "").strip()
            if not property_id:
                property_id = property_id_from_href(str(row.get("editHref") or ""))
            for key_name in ("name", "full_address", "property_name", "slug"):
                key = str(row.get(key_name) or "").strip()
                if property_id and key:
                    candidates.append({"source": "portfolio_map", "key": key, "property_id": property_id, "normalized": normalize(key)})
    if skill_map and skill_map.is_file():
        data = load_json(skill_map)
        rows = data.get("properties") if isinstance(data, dict) else data
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            property_id = str(row.get("lofty_property_id") or "")
            for key_name in ("full_address", "property_name", "slug"):
                key = str(row.get(key_name) or "")
                if property_id and key:
                    candidates.append({"source": "skill_map", "key": key, "property_id": property_id, "normalized": normalize(key)})
    return candidates


def match_property_id(property_path: Path, candidates: list[dict[str, str]]) -> tuple[str | None, dict[str, Any]]:
    target = normalize(property_path.name)
    target_names = [target]
    if property_path.name.endswith(" Public"):
        stripped_target = normalize(property_path.name.removesuffix(" Public"))
        if stripped_target and stripped_target not in target_names:
            target_names.append(stripped_target)
    matches: list[tuple[int, dict[str, str]]] = []
    for candidate in candidates:
        key = candidate["normalized"]
        if not key:
            continue
        matched_target = next((item for item in target_names if key == item or key in item or item in key), "")
        if matched_target:
            score = len(key) + (1000 if key == matched_target else 0) + (100 if candidate["source"] == "portfolio_map" else 0)
            matches.append((score, candidate))
    matches.sort(key=lambda item: item[0], reverse=True)
    if not matches:
        return None, {"match_status": "unmatched", "normalized_property": target}
    top_score, top = matches[0]
    ambiguous = [candidate for score, candidate in matches if score == top_score and candidate["property_id"] != top["property_id"]]
    if ambiguous:
        return None, {"match_status": "ambiguous", "normalized_property": target, "candidates": [top, *ambiguous]}
    return top["property_id"], {"match_status": "matched", "match_source": top["source"], "match_key": top["key"]}


def externally_excluded_records(rows: list[dict[str, str]], exclusion_guards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in rows:
        if not is_active_index_status(row.get("status")):
            continue
        property_path, path_resolution = resolve_index_property_path(row)
        exclusion = match_exclusion_guard(property_path, exclusion_guards)
        if not exclusion:
            continue
        records.append(
            {
                "status": "excluded_no_live_update_or_email",
                "raw_status": str(row.get("status") or ""),
                "property_path": str(property_path),
                "property_name": property_path.name,
                "exclude_source": exclusion.get("source"),
                "exclude_reason": exclusion.get("exclude_reason"),
                "matched_exclusion_property": exclusion.get("property_name"),
                "yhome_column_b": exclusion.get("yhome_column_b"),
                **path_resolution,
            }
        )
    return records


def index_targets(
    rows: list[dict[str, str]],
    candidates: list[dict[str, str]],
    exclusion_guards: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for row in rows:
        if not is_active_index_status(row.get("status")):
            continue
        property_path, path_resolution = resolve_index_property_path(row)
        if match_exclusion_guard(property_path, exclusion_guards or []):
            continue
        updates_md = public_dir_for_property(property_path) / UPDATES_DIR_NAME / "UPDATES.md"
        property_id, match = match_property_id(property_path, candidates)
        property_name = display_name_for_property_path(property_path, path_resolution)
        targets.append(
            {
                "property_path": str(property_path),
                "property_name": property_name,
                "updates_md": str(updates_md),
                "lofty_property_id": property_id,
                **path_resolution,
                **match,
            }
        )
    return targets


def skipped_index_records(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in rows:
        if not is_excluded_index_status(row.get("status")):
            continue
        property_path, path_resolution = resolve_index_property_path(row)
        records.append(
            {
                "status": normalize_index_status(row.get("status")),
                "raw_status": str(row.get("status") or ""),
                "property_path": str(property_path),
                **path_resolution,
            }
        )
    return records


def extract_properties(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict) and isinstance(data.get("properties"), list):
        return data["properties"]
    if isinstance(payload, dict) and isinstance(payload.get("properties"), list):
        return payload["properties"]
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        return payload["items"]
    return []


def property_id_for_api_row(row: dict[str, Any]) -> str:
    for key in ("id", "propertyId", "property_id"):
        value = row.get(key)
        if value:
            return str(value)
    return ""


def live_updates_for_property(row: dict[str, Any]) -> str:
    for key in ("updates", "update", "propertyUpdates", "latestUpdates"):
        value = row.get(key)
        if isinstance(value, str):
            return value.strip()
    return ""


def fetch_manager_properties(skill_scripts_dir: Path, year: int, month: int, close_extra_tabs: bool) -> tuple[dict[str, Any] | None, str | None]:
    sys.path.insert(0, str(skill_scripts_dir))
    try:
        import update_lofty_pm_property as lofty_pm  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return None, f"failed to import Lofty PM helpers: {exc}"
    request_get_manager_properties_via_turbopack_bridge = getattr(
        lofty_pm,
        "request_get_manager_properties_via_turbopack_bridge",
        getattr(lofty_pm, "get_manager_properties_data_via_turbopack_bridge", None),
    )
    payload = {"year": str(year), "month": str(month)}
    bridge_error: str | None = None
    if request_get_manager_properties_via_turbopack_bridge is not None:
        try:
            bridge = request_get_manager_properties_via_turbopack_bridge(
                payload,
                close_extra_tabs=close_extra_tabs,
            )
            data = bridge.get("response") if isinstance(bridge, dict) else None
            if data is None and isinstance(bridge, tuple) and bridge and isinstance(bridge[0], dict):
                data = bridge[0]
            if isinstance(data, dict):
                return data, None
            bridge_error = f"Lofty PM Turbopack fetch returned {type(data).__name__}"
        except Exception as exc:  # noqa: BLE001
            bridge_error = f"Lofty PM Turbopack fetch failed: {exc}"
    try:
        build_headers = getattr(lofty_pm, "build_headers")
        capture_fresh = getattr(lofty_pm, "capture_fresh")
        request = getattr(lofty_pm, "request")
        headers = build_headers(capture_fresh("get-manager-properties", close_extra_tabs=close_extra_tabs, payload=payload))
        response = request("GET", "https://api.lofty.ai/prod/property-managers/v2/get-manager-properties", headers, payload)
    except Exception as exc:  # noqa: BLE001
        detail = f"Lofty PM API fetch failed: {exc}"
        return None, f"{detail}; {bridge_error}" if bridge_error else detail
    if not response.ok:
        detail = f"Lofty PM API fetch failed: HTTP {response.status_code} {response.text[:500]}"
        return None, f"{detail}; {bridge_error}" if bridge_error else detail
    return response.json(), None


def run_guard(command: list[str]) -> dict[str, Any]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=GUARD_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "return_code": 124,
            "ok": False,
            "stdout_tail": (exc.stdout or "")[-2000:] if isinstance(exc.stdout, str) else "",
            "stderr_tail": f"guard command timed out after {GUARD_TIMEOUT_SECONDS}s",
            "timed_out": True,
        }
    return {
        "command": command,
        "return_code": result.returncode,
        "ok": result.returncode == 0,
        "stdout_tail": result.stdout[-2000:],
        "stderr_tail": result.stderr[-2000:],
    }


def parse_live_snapshot_listing_issues(*guard_results: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for result in guard_results:
        text = "\n".join([str(result.get("stdout_tail") or ""), str(result.get("stderr_tail") or "")])
        for match in re.finditer(r"live_snapshot_listing_issues=([^\n]+)", text):
            for raw_issue in match.group(1).split(","):
                issue = raw_issue.strip()
                if issue and issue not in issues:
                    issues.append(issue)
    return issues


def live_snapshot_listing_issue_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        for issue in record.get("live_snapshot_listing_issues") or []:
            key = str(issue).split("=", 1)[0]
            if key:
                counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def shell_command(parts: list[object]) -> str:
    return " ".join(shlex.quote(str(part)) for part in parts if str(part or "").strip())


def safe_monthly_cron_dry_run_command() -> str:
    env_root = os.environ.get("OPENCLAW_WORKSPACE") or os.environ.get("WORKSPACE_ROOT")
    if env_root:
        return f"cd {shlex.quote(str(Path(env_root)))} && {SAFE_MONTHLY_CRON_DRY_RUN_COMMAND}"
    cwd = Path.cwd()
    root = cwd if (cwd / "scripts" / "baselane_financials_monthly_cron.sh").is_file() else Path(__file__).absolute().parents[1]
    return f"cd {shlex.quote(str(root))} && {SAFE_MONTHLY_CRON_DRY_RUN_COMMAND}"


def capture_rerun_command(args: argparse.Namespace, *, apply: bool) -> str:
    parts: list[object] = [
        "python3",
        Path("scripts") / Path(__file__).name,
        "--index-csv",
        args.index_csv,
        "--report",
        args.report,
        "--updates-guard",
        args.updates_guard,
        "--skill-scripts-dir",
        args.skill_scripts_dir,
        "--artifact-dir",
        args.artifact_dir,
        "--year",
        args.year,
        "--month",
        args.month,
    ]
    if args.portfolio_map:
        parts.extend(["--portfolio-map", args.portfolio_map])
    if args.skill_map:
        parts.extend(["--skill-map", args.skill_map])
    if args.max_properties:
        parts.extend(["--max-properties", args.max_properties])
    if args.yhome_transition_csv:
        parts.extend(["--yhome-transition-csv", args.yhome_transition_csv])
    for property_name in args.manual_excluded_property or []:
        parts.extend(["--manual-excluded-property", property_name])
    if apply:
        parts.append("--apply")
    if args.close_extra_tabs:
        parts.append("--close-extra-tabs")
    return shell_command(parts)


def is_lofty_auth_issue(issue: str) -> bool:
    text = str(issue or "").lower()
    return (
        "lofty pm api fetch failed" in text
        and (
            "unauthorized" in text
            or "http 401" in text
            or '"code":401' in text
            or '"httpcode":401' in text
            or "'code': 401" in text
            or "'httpcode': 401" in text
        )
    )


def is_lofty_capture_transport_issue(issue: str) -> bool:
    text = str(issue or "").lower()
    return "lofty pm api fetch failed" in text and (
        "fresh auth capture failed" in text
        or "did not capture a signed lofty api request" in text
        or "turbopack runtime not available" in text
        or "turbopack bridge failed" in text
    )


def lofty_preflight_recovery_exhausted(report_path: Path) -> bool:
    preflight_path = report_path.parent / "lofty_cdp_preflight_report.json"
    if not preflight_path.is_file():
        return False
    try:
        preflight = load_json(preflight_path)
    except Exception:
        return False
    if not isinstance(preflight, dict):
        return False
    return bool(
        preflight.get("automated_browser_recovery_complete")
        or preflight.get("login_recovery_exhausted")
        or preflight.get("manual_auth_phase") == "after_browser_recovery"
    )


def lofty_cdp_recovery_action(args: argparse.Namespace) -> str:
    if lofty_preflight_recovery_exhausted(args.report):
        return LOFTY_VISIBLE_AUTH_ACTION
    return LOFTY_CDP_RECOVERY_ACTION


def report_next_action(status: str, args: argparse.Namespace, issues: list[str], capture_ready: bool) -> dict[str, Any]:
    rerun_command = safe_monthly_cron_dry_run_command()
    recovery_action = lofty_cdp_recovery_action(args)
    if capture_ready:
        return {
            "status": "ready",
            "summary": "Live Lofty UPDATES.md guard capture is current for all active targets.",
            "rerun_command": rerun_command,
            "requires_authenticated_cdp": False,
            "holds_live_publish_and_owner_email": False,
        }
    if issues:
        if is_lofty_auth_issue(issues[0]):
            return {
                "status": "fix_capture_prerequisite",
                "summary": recovery_action,
                "diagnostic": issues[0],
                "auth_issue_class": "lofty_pm_unauthorized",
                "rerun_command": rerun_command,
                "requires_authenticated_cdp": True,
                "holds_live_publish_and_owner_email": True,
            }
        if is_lofty_capture_transport_issue(issues[0]):
            return {
                "status": "fix_capture_prerequisite",
                "summary": recovery_action,
                "diagnostic": issues[0],
                "capture_issue_class": "lofty_pm_capture_transport_unavailable",
                "rerun_command": rerun_command,
                "requires_authenticated_cdp": True,
                "holds_live_publish_and_owner_email": True,
            }
        return {
            "status": "fix_capture_prerequisite",
            "summary": issues[0],
            "rerun_command": rerun_command,
            "requires_authenticated_cdp": bool(args.apply),
            "holds_live_publish_and_owner_email": True,
        }
    if not args.apply:
        return {
            "status": "capture_authenticated_live_updates",
            "summary": recovery_action,
            "rerun_command": rerun_command,
            "requires_authenticated_cdp": True,
            "holds_live_publish_and_owner_email": True,
        }
    return {
        "status": "reconcile_live_update_guards" if status == "review" else "review_live_update_capture_failure",
        "summary": "Use records[].next_action_command for each unverified UPDATES.md target, then rerun capture.",
        "rerun_command": rerun_command,
        "requires_authenticated_cdp": True,
        "holds_live_publish_and_owner_email": True,
    }


def status_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        status = str(record.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def review_blockers(
    *,
    apply: bool,
    planned_count: int,
    blocked_count: int,
    mismatch_count: int,
    unverified_count: int,
) -> list[str]:
    blockers: list[str] = []
    if not apply:
        blockers.append("live_update_capture_not_applied")
    if planned_count:
        blockers.append(f"live_update_planned_count={planned_count}")
    if blocked_count:
        blockers.append(f"live_update_blocked_count={blocked_count}")
    if mismatch_count:
        blockers.append(f"live_update_mismatch_count={mismatch_count}")
    if unverified_count:
        blockers.append(f"live_update_unverified_count={unverified_count}")
    return blockers


def add_next_action(record: dict[str, Any], updates_guard: Path) -> None:
    status = str(record.get("status") or "")
    updates_md = record.get("updates_md")
    snapshot_path = record.get("snapshot_path")
    if status == "blocked_no_property_id":
        record.update(
            {
                "next_action_stage": "map_lofty_property_id",
                "next_action_file": record.get("property_path") or "",
                "next_action_command": "",
                "next_action_detail": "Add or fix this property's Lofty property id in the portfolio/skill map before live capture.",
            }
        )
        return
    if status == "blocked_missing_updates_md":
        record.update(
            {
                "next_action_stage": "restore_updates_md",
                "next_action_file": updates_md or "",
                "next_action_command": "",
                "next_action_detail": "Restore the canonical Public/00 - README & Property Snapshot/UPDATES.md before live capture.",
            }
        )
        return
    if status in {"planned", "blocked_missing_live_api_row", "needs_reconcile"}:
        record.update(
            {
                "next_action_stage": "capture_update_live_guard",
                "next_action_file": snapshot_path or updates_md or "",
                "next_action_command": shell_command(
                    [
                        updates_guard,
                        "capture-fetch",
                        updates_md,
                        snapshot_path,
                        "--source",
                        "Lofty PM get-manager-properties updates field",
                    ]
                ),
                "next_action_detail": "Fetch the live Lofty UPDATES.md text, register it with the guard, then run the UPDATES.md guard check.",
            }
        )
        return
    if status == "guard_ok":
        record.update(
            {
                "next_action_stage": "",
                "next_action_file": "",
                "next_action_command": "",
                "next_action_detail": "Live UPDATES.md guard verified.",
            }
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture live Lofty PM updates field text and register UPDATES.md live-fetch guard artifacts.")
    parser.add_argument("--index-csv", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--portfolio-map", type=Path)
    parser.add_argument("--skill-map", type=Path)
    parser.add_argument("--updates-guard", required=True, type=Path)
    parser.add_argument("--skill-scripts-dir", required=True, type=Path)
    parser.add_argument("--artifact-dir", required=True, type=Path)
    parser.add_argument("--year", type=int, default=datetime.now().year)
    parser.add_argument("--month", type=int, default=datetime.now().month)
    parser.add_argument("--max-properties", type=int, default=0)
    parser.add_argument("--property", action="append", default=[], help="Limit capture to an exact normalized property name; repeatable.")
    parser.add_argument("--yhome-transition-csv", type=Path)
    parser.add_argument("--manual-excluded-property", action="append", default=[])
    parser.add_argument("--transfer-reconciliation-report", type=Path)
    parser.add_argument("--guarded-apply-report", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--close-extra-tabs", action="store_true")
    args = parser.parse_args()

    issues: list[str] = []
    if not args.index_csv.is_file():
        issues.append(f"monthly index missing: {args.index_csv}")
    if not args.updates_guard.is_file():
        issues.append(f"updates guard missing: {args.updates_guard}")
    if not (args.skill_scripts_dir / "update_lofty_pm_property.py").is_file():
        issues.append(f"Lofty PM helper missing: {args.skill_scripts_dir / 'update_lofty_pm_property.py'}")

    rows = load_index(args.index_csv) if args.index_csv.is_file() else []
    rows = filter_rows_by_property(rows, args.property)
    if args.index_csv.is_file() and not rows:
        issues.append(f"monthly index has no property rows: {args.index_csv}")
    skipped_records = skipped_index_records(rows)
    manual_names = [*DEFAULT_MANUAL_EXCLUDED_PROPERTIES, *args.manual_excluded_property]
    exclusion_guards, yhome_guard, manual_exclusions = monthly_exclusion_guards(
        args.yhome_transition_csv,
        manual_names,
    )
    target_exclusion_guards, _, _ = monthly_exclusion_guards(args.yhome_transition_csv, manual_names)
    financial_hold_exclusions = financial_hold_exclusion_records(args.transfer_reconciliation_report)
    guarded_apply_exclusions = guarded_apply_exclusion_records(args.guarded_apply_report)
    exclusion_guards.extend(financial_hold_exclusions)
    target_exclusion_guards.extend([*financial_hold_exclusions, *guarded_apply_exclusions])
    external_excluded_records = externally_excluded_records(rows, exclusion_guards)
    append_unmapped_exclusion_records(
        external_excluded_records,
        guarded_apply_exclusions,
        represented_records=skipped_records,
    )
    candidates = property_id_candidates(args.portfolio_map, args.skill_map)
    targets = index_targets(rows, candidates, target_exclusion_guards)
    if args.max_properties > 0:
        targets = targets[: args.max_properties]
    current_only_verified = load_current_only_verify(Path.cwd(), len(targets))
    run_month = f"{args.year:04d}-{args.month:02d}"

    api_payload: dict[str, Any] | None = None
    api_error: str | None = None
    live_by_id: dict[str, dict[str, Any]] = {}
    if args.apply and not issues:
        api_payload, api_error = fetch_manager_properties(args.skill_scripts_dir, args.year, args.month, args.close_extra_tabs)
        if api_error:
            issues.append(api_error)
        else:
            for api_row in extract_properties(api_payload or {}):
                property_id = property_id_for_api_row(api_row)
                if property_id:
                    live_by_id[property_id] = api_row

    records: list[dict[str, Any]] = []
    register_count = 0
    check_ok_count = 0
    mismatch_count = 0
    for target in targets:
        record = dict(target)
        property_id = target.get("lofty_property_id")
        updates_md = Path(target["updates_md"])
        if not property_id:
            record["status"] = "blocked_no_property_id"
            add_next_action(record, args.updates_guard)
            records.append(record)
            continue
        if not updates_md.is_file():
            record["status"] = "blocked_missing_updates_md"
            add_next_action(record, args.updates_guard)
            records.append(record)
            continue
        snapshot_path = args.artifact_dir / property_id / "live-UPDATES.md"
        record["snapshot_path"] = str(snapshot_path)
        if not args.apply or issues:
            record["status"] = "planned"
            add_next_action(record, args.updates_guard)
            records.append(record)
            continue
        live_row = live_by_id.get(str(property_id))
        if not live_row:
            record["status"] = "blocked_missing_live_api_row"
            add_next_action(record, args.updates_guard)
            records.append(record)
            continue
        live_text = live_updates_for_property(live_row)
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(live_text + ("\n" if live_text else ""), encoding="utf-8")
        record["live_updates_length"] = len(live_text)
        register = run_guard(
            [
                sys.executable,
                str(args.updates_guard),
                "capture-fetch",
                str(updates_md),
                str(snapshot_path),
                "--source",
                "Lofty PM get-manager-properties updates field",
            ]
        )
        record["register"] = register
        if register["ok"]:
            register_count += 1
        check = run_guard([sys.executable, str(args.updates_guard), "check", str(updates_md)])
        record["check"] = check
        listing_issues = parse_live_snapshot_listing_issues(register, check)
        if listing_issues:
            record["live_snapshot_listing_issue_count"] = len(listing_issues)
            record["live_snapshot_listing_issues"] = listing_issues
        full_history = full_history_containment(updates_md, live_text, args.skill_scripts_dir)
        record["full_history_containment"] = full_history
        if check["ok"] and full_history.get("ok") is True:
            check_ok_count += 1
            record["status"] = "guard_ok"
        else:
            mismatch_count += 1
            record["status"] = "needs_reconcile"
            if check["ok"] and full_history.get("ok") is not True:
                record["listing_update_restore_required"] = True
                record["listing_update_restore_reason"] = "live_listing_missing_canonical_full_history_entries"
            if str(property_id) in current_only_verified or publish_state_current_only_verify(updates_md, str(property_id), live_text, run_month):
                record["listing_update_restore_required"] = True
                record["listing_update_restore_reason"] = "live_listing_contains_current_only_update_not_full_history"
        add_next_action(record, args.updates_guard)
        records.append(record)

    planned_count = sum(1 for record in records if record.get("status") == "planned")
    blocked_count = sum(1 for record in records if str(record.get("status", "")).startswith("blocked_"))
    unverified_count = max(0, len(targets) - check_ok_count)
    target_digest = stable_digest(
        {
            "records": [
                {
                    "property_name": record.get("property_name"),
                    "property_path": record.get("property_path"),
                    "updates_md": record.get("updates_md"),
                    "lofty_property_id": record.get("lofty_property_id"),
                    "status": record.get("status"),
                    "snapshot_path": record.get("snapshot_path"),
                    "next_action_stage": record.get("next_action_stage"),
                    "next_action_file": record.get("next_action_file"),
                    "next_action_command": record.get("next_action_command"),
                }
                for record in records
            ]
        }
    )
    capture_ready = bool(args.apply) and not issues and not planned_count and not blocked_count and not mismatch_count and check_ok_count == len(targets)
    status = "failed" if issues and args.apply else "ok" if capture_ready else "review"
    next_action = report_next_action(status, args, issues, capture_ready)
    review_blocker_list = (
        []
        if status == "ok"
        else review_blockers(
            apply=args.apply,
            planned_count=planned_count,
            blocked_count=blocked_count,
            mismatch_count=mismatch_count,
            unverified_count=unverified_count,
        )
    )
    record_status_counts = status_counts(records)
    listing_issue_counts = live_snapshot_listing_issue_counts(records)
    listing_issue_property_count = sum(1 for record in records if record.get("live_snapshot_listing_issues"))
    full_history_missing_property_count = sum(
        1
        for record in records
        if isinstance(record.get("full_history_containment"), dict)
        and record["full_history_containment"].get("ok") is not True
    )
    full_history_missing_entry_count = sum(
        int(record["full_history_containment"].get("missing_entry_count") or 0)
        for record in records
        if isinstance(record.get("full_history_containment"), dict)
    )
    report = {
        "generated_at": iso_z(),
        "status": status,
        "apply": args.apply,
        "live_capture": args.apply,
        "mutates_lofty_listing": False,
        "mutates_external_system": False,
        "external_mutation_count": 0,
        "capture_semantics": "authenticated_read_and_guard_registration_only",
        "sends_owner_email": False,
        "year": args.year,
        "month": args.month,
        "issues": issues,
        "issue_count": len(issues),
        "review_blockers": review_blocker_list,
        "review_blocker_count": len(review_blocker_list),
        "review_blocker_summary": review_blocker_list[0] if review_blocker_list else None,
        "next_action": next_action,
        "rerun_command": next_action["rerun_command"],
        "requires_authenticated_cdp": next_action["requires_authenticated_cdp"],
        "holds_live_publish_and_owner_email": next_action["holds_live_publish_and_owner_email"],
        "target_count": len(targets),
        "skipped_index_count": len(skipped_records),
        "skipped_index_status_counts": {
            status: sum(1 for record in skipped_records if record.get("status") == status)
            for status in sorted({str(record.get("status") or "") for record in skipped_records})
        },
        "skipped_index_digest": stable_digest({"records": skipped_records}),
        "skipped_index_records": skipped_records,
        "externally_excluded_count": len(external_excluded_records),
        "externally_excluded_records": external_excluded_records,
        "excluded_property_count": len(skipped_records) + len(external_excluded_records),
        "excluded_property_names": [
            *[Path(str(record.get("property_path") or "")).name for record in skipped_records],
            *[str(record.get("property_name") or "") for record in external_excluded_records],
        ],
        "yhome_transition_guard": yhome_guard,
        "manual_excluded_property_names": [record["property_name"] for record in manual_exclusions],
        "planned_count": planned_count,
        "blocked_count": blocked_count,
        "register_count": register_count,
        "check_ok_count": check_ok_count,
        "required_check_ok_count": len(targets),
        "unverified_count": unverified_count,
        "mismatch_count": mismatch_count,
        "live_snapshot_listing_issue_property_count": listing_issue_property_count,
        "live_snapshot_listing_issue_counts": listing_issue_counts,
        "full_history_containment_property_count": len(
            [record for record in records if isinstance(record.get("full_history_containment"), dict)]
        ),
        "full_history_missing_property_count": full_history_missing_property_count,
        "full_history_missing_entry_count": full_history_missing_entry_count,
        "record_status_counts": record_status_counts,
        "target_digest": target_digest,
        "capture_contract": {
            "ready": capture_ready,
            "apply": args.apply,
            "live_capture": args.apply,
            "mutates_lofty_listing": False,
            "mutates_external_system": False,
            "external_mutation_count": 0,
            "capture_semantics": "authenticated_read_and_guard_registration_only",
            "sends_owner_email": False,
            "target_count": len(targets),
            "register_count": register_count,
            "check_ok_count": check_ok_count,
            "required_check_ok_count": len(targets),
            "planned_count": planned_count,
            "blocked_count": blocked_count,
            "mismatch_count": mismatch_count,
            "live_snapshot_listing_issue_property_count": listing_issue_property_count,
            "live_snapshot_listing_issue_counts": listing_issue_counts,
            "full_history_missing_property_count": full_history_missing_property_count,
            "full_history_missing_entry_count": full_history_missing_entry_count,
            "unverified_count": unverified_count,
            "review_blocker_count": len(review_blocker_list),
            "review_blockers": review_blocker_list,
            "record_status_counts": record_status_counts,
            "target_digest": target_digest,
        },
        "records": records,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if status == "ok":
        verify_records = []
        for record in records:
            proof = record.get("current_only_listing_verify") if isinstance(record.get("current_only_listing_verify"), dict) else None
            if record.get("status") == "guard_ok_current_only" and proof:
                verify_records.append(
                    {
                        "property_name": record.get("property_name"),
                        "lofty_property_id": record.get("lofty_property_id"),
                        "snapshot_path": record.get("snapshot_path"),
                        "approved_update_source": proof.get("publish_state_file"),
                        "ok": True,
                        "financial_summary_verified": True,
                        **{key: proof.get(key) for key in ("expected_char_count", "live_char_count", "expected_line_count", "live_line_count", "expected_sha256", "live_sha256")},
                    }
                )
    print(json.dumps({key: report[key] for key in ("status", "issue_count", "review_blocker_count", "target_count", "register_count", "check_ok_count", "unverified_count", "mismatch_count")}, indent=2, sort_keys=True))
    return 0 if status == "ok" else 2 if status == "review" else 1


if __name__ == "__main__":
    raise SystemExit(main())
