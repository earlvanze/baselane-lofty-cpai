#!/usr/bin/env python3
"""Refresh Lofty Operating Cash and ECO Net DAO Funds in Yhome.

The monthly candidate packet provides property and canonical GL coverage.  This
daily wrapper replaces only the Lofty reserve fields with a fresh, read-only
get-manager-properties response before delegating the guarded Google Sheets
apply/verify workflow. Before a sheet write, the verifier replaces legacy
full-property-GL ECO rows with the ECO-custody policy: ECO-owned bank activity
plus only negative Yhome Net Due TO DAO balances. An unavailable or incomplete
live Lofty response is a hard write gate, not a reason to reuse an old reserve
snapshot.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lofty_monthly_review_candidate_packet import names_match, parse_money


SHEET_SPECS = ("Cleveland=1187056671", "Chicago & non-Yhome=433920866", "Yhome Deeded & Sold=1902489452")


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def reserve_rows(value: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(value, dict):
        address = value.get("address") or value.get("fullAddress") or value.get("name")
        reserve = parse_money(value.get("curr_maintenance_reserve"))
        if address and reserve is not None:
            rows.append({"address": str(address), "curr_maintenance_reserve": reserve})
        for child in value.values():
            rows.extend(reserve_rows(child))
    elif isinstance(value, list):
        for child in value:
            rows.extend(reserve_rows(child))
    return rows


def addresses_match(left: str, right: str) -> bool:
    if names_match(left, right):
        return True
    aliases = {
        "avenue": "ave",
        "boulevard": "blvd",
        "court": "ct",
        "drive": "dr",
        "place": "pl",
        "road": "rd",
        "street": "st",
    }

    def normalized(value: str) -> str:
        text = str(value or "").lower()
        for long, short in aliases.items():
            text = re.sub(rf"\b{long}\b", short, text)
        return re.sub(r"[^a-z0-9]+", " ", text).strip()

    left_normalized, right_normalized = normalized(left), normalized(right)
    return bool(left_normalized and right_normalized and (left_normalized in right_normalized or right_normalized in left_normalized))


def capture_live_snapshot(args: argparse.Namespace) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    payload_path = ROOT / "reports/lofty-pm-current/get-manager-properties.daily-yhome.payload.json"
    response_path = ROOT / "reports/lofty-pm-current/get-manager-properties.daily-yhome.response.json"
    write_json(payload_path, {"year": args.year, "month": args.month_number})
    command = [
        sys.executable,
        str(ROOT / "skills/lofty-pm/scripts/update_lofty_pm_property.py"),
        "--payload-file",
        str(payload_path),
        "--kind",
        "get-manager-properties",
        "--refresh-on-demand",
        "--retry-on-auth-failure",
        "--close-extra-tabs",
        "--response-file",
        str(response_path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=args.lofty_timeout_seconds, check=False)
    except subprocess.TimeoutExpired:
        return None, {"status": "failed", "reason": "live_lofty_snapshot_timeout", "command": command}
    if result.returncode != 0:
        return None, {"status": "failed", "reason": "live_lofty_snapshot_command_failed", "returncode": result.returncode, "stderr": result.stderr[-1000:], "command": command}
    try:
        payload = read_json(response_path)
    except (OSError, json.JSONDecodeError):
        return None, {"status": "failed", "reason": "live_lofty_snapshot_response_missing", "stdout": result.stdout[-1000:], "stderr": result.stderr[-1000:], "command": command}
    rows = reserve_rows(payload)
    if not rows:
        return None, {"status": "failed", "reason": "live_lofty_snapshot_has_no_reserves", "command": command}
    return payload, {"status": "ok", "property_count": len(rows), "response_path": str(response_path), "command": command}


def build_daily_packet(packet: dict[str, Any], snapshot: dict[str, Any], output: Path) -> dict[str, Any]:
    live = reserve_rows(snapshot)
    records = packet.get("records") if isinstance(packet, dict) else None
    if not isinstance(records, list) or not records:
        raise ValueError("candidate packet has no records")
    missing: list[str] = []
    updated = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        property_name = str(record.get("property_name") or record.get("input_property_name") or "").strip()
        summary = record.get("monthly_financial_summary")
        if not isinstance(summary, dict):
            continue
        matched = next((row for row in live if addresses_match(property_name, row["address"])), None)
        if matched is None:
            missing.append(property_name)
            continue
        summary["lofty_curr_maintenance_reserve"] = matched["curr_maintenance_reserve"]
        summary["lofty_curr_maintenance_reserve_source"] = matched["address"]
        summary["lofty_curr_maintenance_reserve_source_file"] = "live Lofty get-manager-properties daily snapshot"
        summary["lofty_curr_maintenance_reserve_source_mode"] = "daily_live_lofty_snapshot"
        updated += 1
    if missing:
        raise ValueError("live Lofty reserve missing candidate properties: " + ", ".join(sorted(set(missing))[:20]))
    packet["daily_yhome_live_lofty_snapshot_generated_at"] = now()
    packet["daily_yhome_live_lofty_reserve_updates"] = updated
    write_json(output, packet)
    return {"candidate_record_count": len(records), "live_reserve_update_count": updated, "daily_candidate_packet": str(output)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", default=datetime.now().strftime("%Y-%m"))
    parser.add_argument("--candidate-packet", type=Path, default=ROOT / "reports/baselane_financials_monthly_review_candidate_packet.json")
    parser.add_argument("--daily-candidate-packet", type=Path, default=ROOT / "reports/baselane_daily_yhome_candidate_packet.json")
    parser.add_argument("--report", type=Path, default=ROOT / "reports/baselane_daily_yhome_operating_cash_sync.json")
    parser.add_argument("--lofty-timeout-seconds", type=float, default=float(os.environ.get("YHOME_DAILY_LOFTY_TIMEOUT_SECONDS") or 90))
    args = parser.parse_args()
    args.year, month_text = args.month.split("-", 1)
    args.year = int(args.year)
    args.month_number = int(month_text)
    return args


def main() -> int:
    args = parse_args()
    report: dict[str, Any] = {"job": "baselane-daily-yhome-operating-cash-sync", "generated_at": now(), "month": args.month, "status": "review", "external_write_attempted": False, "target_columns": ["Lofty Operating Cash", "ECO Net DAO Funds"]}
    try:
        packet = read_json(args.candidate_packet)
        snapshot, capture = capture_live_snapshot(args)
        report["live_lofty_capture"] = capture
        if snapshot is None:
            report["reason"] = capture["reason"]
            write_json(args.report, report)
            return 1
        report.update(build_daily_packet(packet, snapshot, args.daily_candidate_packet))
    except Exception as exc:  # noqa: BLE001
        report["reason"] = "daily_candidate_packet_not_ready"
        report["error"] = str(exc)
        write_json(args.report, report)
        return 1

    verifier = ROOT / "scripts/yhome_operating_cash_apply_verify.py"
    verifier_report = ROOT / "reports/yhome_operating_cash_daily_apply_verify_report.json"
    command = [sys.executable, str(verifier), "--month", args.month, "--candidate-packet", str(args.daily_candidate_packet), "--yhome-export-url", "", "--yhome-gws-sheet-spec", SHEET_SPECS[0], "--yhome-gws-sheet-spec", SHEET_SPECS[1], "--yhome-gws-sheet-spec", SHEET_SPECS[2], "--audit-report", str(ROOT / "reports/baselane_cf_balance_sheet_consistency_daily.json"), "--plan-csv", str(ROOT / "reports/yhome_operating_cash_daily_update_plan.csv"), "--yhome-missing-candidates-csv", str(ROOT / "reports/yhome_daily_missing_candidates.csv"), "--updater-report", str(ROOT / "reports/yhome_operating_cash_daily_gsheet_update_report.json"), "--report", str(verifier_report)]
    apply = os.environ.get("YHOME_GSHEET_APPLY") == "1" and os.environ.get("YHOME_GSHEET_WRITE_ENABLED") == "1"
    if apply:
        command.append("--apply")
    result = subprocess.run(command, check=False)
    verifier_payload = read_json(verifier_report) if verifier_report.is_file() else {}
    report.update({"apply_requested": apply, "verifier_report": str(verifier_report), "verifier_status": verifier_payload.get("status"), "verifier_reason": verifier_payload.get("reason"), "external_write_attempted": bool(verifier_payload.get("external_write_attempted")), "status": "ok" if result.returncode == 0 else "review", "reason": verifier_payload.get("reason") or "verifier_failed"})
    write_json(args.report, report)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
