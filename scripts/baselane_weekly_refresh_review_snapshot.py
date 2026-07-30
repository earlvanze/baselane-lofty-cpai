#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "missing", "path": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"status": "unreadable", "path": str(path), "error": str(exc)}
    return data if isinstance(data, dict) else {"status": "unreadable", "path": str(path), "error": "not a JSON object"}


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def refresh_snapshot(report_path: Path, gate_path: Path) -> dict[str, Any]:
    report = read_json(report_path)
    gate = read_json(gate_path)
    if report.get("status") in {"missing", "unreadable"}:
        return {"status": "error", "reason": f"report_{report.get('status')}", "report_path": str(report_path)}
    if gate.get("status") in {"missing", "unreadable"}:
        return {"status": "error", "reason": f"gate_{gate.get('status')}", "gate_path": str(gate_path)}

    previous = {
        "cf_review_gate_status": report.get("cf_review_gate_status"),
        "cf_review_gate_blocker_count": report.get("cf_review_gate_blocker_count"),
        "cf_review_gate_idempotency_key": report.get("cf_review_gate_idempotency_key"),
        "cf_review_gate_action_queue_digest": report.get("cf_review_gate_action_queue_digest"),
        "cf_review_gate_action_queue_count": report.get("cf_review_gate_action_queue_count"),
    }
    refreshed = {
        "cf_review_gate_status": gate.get("status"),
        "cf_review_gate_blocker_count": gate.get("blocker_count"),
        "cf_review_gate_idempotency_key": gate.get("idempotency_key"),
        "cf_review_gate_action_queue_digest": gate.get("action_queue_digest"),
        "cf_review_gate_action_queue_count": gate.get("action_queue_count") or (gate.get("summary") or {}).get("action_queue_count"),
        "cf_review_gate": str(gate_path),
        "cf_review_gate_markdown": str(gate_path.with_suffix(".md")),
    }

    updated = dict(report)
    updated.update(refreshed)
    review_safe = updated.get("review_safe_idempotency") if isinstance(updated.get("review_safe_idempotency"), dict) else {}
    review_safe = {
        **review_safe,
        "status": updated.get("status"),
        "iso_week": updated.get("iso_week"),
        "last_completed_week": updated.get("last_completed_week"),
        "state_file": updated.get("state_file"),
        "state_file_marked_complete": updated.get("state_file_marked_complete"),
        "state_file_unmarked": updated.get("state_file_unmarked"),
        "state_file_unmarked_reason": updated.get("state_file_unmarked_reason"),
        "safe_to_skip_next_run": updated.get("status") == "ok" and updated.get("state_file_marked_complete") is True,
        "retry_required": updated.get("status") == "review",
        "weekly_unprocessed_idempotent": updated.get("weekly_unprocessed_idempotent"),
        "cf_review_gate_idempotency_key": refreshed["cf_review_gate_idempotency_key"],
        "cf_review_gate_action_queue_digest": refreshed["cf_review_gate_action_queue_digest"],
        "cf_review_gate_action_queue_count": refreshed["cf_review_gate_action_queue_count"],
        "cf_review_gate_snapshot_current": True,
    }
    updated["review_safe_idempotency"] = review_safe
    updated["cf_review_gate_snapshot_refreshed_at"] = iso_z()
    updated["cf_review_gate_snapshot_refresh"] = {
        "status": "ok",
        "previous": previous,
        "current": refreshed,
        "state_file_marked_complete": updated.get("state_file_marked_complete"),
        "deterministic_verification_idempotent": updated.get("deterministic_verification_idempotent"),
        "safe_to_skip_next_run": review_safe["safe_to_skip_next_run"],
        "retry_required": review_safe["retry_required"],
    }
    write_json(report_path, updated)

    changed = previous != {key: refreshed.get(key) for key in previous}
    return {
        "status": "ok",
        "changed": changed,
        "report_path": str(report_path),
        "gate_path": str(gate_path),
        "safe_to_skip_next_run": review_safe["safe_to_skip_next_run"],
        "retry_required": review_safe["retry_required"],
        "cf_review_gate_snapshot_current": True,
        "previous": previous,
        "current": refreshed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh weekly run-report CF review gate snapshot without rerunning weekly mutations.")
    parser.add_argument("--root", type=Path, default=Path(__file__).absolute().parents[1])
    parser.add_argument("--report", type=Path)
    parser.add_argument("--gate", type=Path)
    args = parser.parse_args()

    root = args.root.expanduser().resolve()
    report_path = args.report or root / "reports" / "baselane_weekly_file_updates_run_report.json"
    gate_path = args.gate or root / "reports" / "baselane_weekly_cf_review_gate.json"
    result = refresh_snapshot(report_path, gate_path)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
