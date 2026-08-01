#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_PUBLIC_WORKSPACE = Path("/home/digit/.openclaw/workspace-discord-public")


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def run_public_guard(public_workspace: Path, delete_gl_rows: bool, sanitize_loftyassist_reserves: bool) -> dict[str, Any]:
    guard_script = public_workspace / "scripts" / "discord_public_financial_source_guard.py"
    if not guard_script.is_file():
        return {
            "ok": False,
            "status": "review",
            "issue_count": 1,
            "issues": [
                {
                    "kind": "missing_public_guard_script",
                    "path": str(guard_script),
                    "detail": "workspace-discord-public financial source guard is missing",
                }
            ],
        }
    command = [sys.executable, str(guard_script)]
    if delete_gl_rows:
        command.append("--delete-gl-rows")
    if sanitize_loftyassist_reserves:
        command.append("--sanitize-loftyassist-reserves")
    result = subprocess.run(command, cwd=str(public_workspace), capture_output=True, text=True, timeout=120)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "status": "review",
            "return_code": result.returncode,
            "issue_count": 1,
            "issues": [
                {
                    "kind": "unreadable_public_guard_output",
                    "detail": str(exc),
                    "stdout_tail": result.stdout[-1000:],
                    "stderr_tail": result.stderr[-1000:],
                }
            ],
        }
    payload["return_code"] = result.returncode
    payload["stderr_tail"] = result.stderr[-1000:]
    if result.returncode != 0 and payload.get("ok") is True:
        payload["ok"] = False
        payload["status"] = "review"
        payload["issues"] = list(payload.get("issues") or []) + [
            {"kind": "public_guard_nonzero_return", "detail": f"return_code={result.returncode}"}
        ]
        payload["issue_count"] = len(payload["issues"])
    return payload


def build_report(root: Path, public_workspace: Path, delete_gl_rows: bool, sanitize_loftyassist_reserves: bool) -> dict[str, Any]:
    payload = run_public_guard(public_workspace, delete_gl_rows, sanitize_loftyassist_reserves)
    issues = payload.get("issues") if isinstance(payload.get("issues"), list) else []
    issue_count = int(payload.get("issue_count") or len(issues))
    ok = payload.get("ok") is True and issue_count == 0
    return {
        "generated_at": iso_z(),
        "status": "ok" if ok else "review",
        "ok": ok,
        "root": str(root),
        "public_workspace": str(public_workspace),
        "policy": "Discord public financial data must come only from Dropbox-sourced property Public folders; GL Rows.csv is deleted on sight.",
        "delete_gl_rows": delete_gl_rows,
        "sanitize_loftyassist_reserves": sanitize_loftyassist_reserves,
        "canonical_financial_dir": payload.get("canonical_financial_dir"),
        "canonical_snapshot_dir": payload.get("canonical_snapshot_dir"),
        "financial_source_policy_ok": bool(payload.get("financial_source_policy_ok")) and ok,
        "financial_doc_count": int(payload.get("financial_doc_count") or 0),
        "update_doc_count": int(payload.get("update_doc_count") or 0),
        "legacy_financials_folder_count": int(payload.get("legacy_financials_folder_count") or 0),
        "legacy_financials_folder_ignored_count": int(payload.get("legacy_financials_folder_ignored_count") or 0),
        "legacy_financials_folder_policy": payload.get("legacy_financials_folder_policy") or "ignored_not_source",
        "deleted_gl_rows_count": int(payload.get("deleted_gl_rows_count") or 0),
        "sanitized_loftyassist_reserve_snapshot_count": int(payload.get("sanitized_loftyassist_reserve_snapshot_count") or 0),
        "removed_loftyassist_curr_maintenance_reserve_count": int(payload.get("removed_loftyassist_curr_maintenance_reserve_count") or 0),
        "issue_count": issue_count,
        "issues": issues,
        "public_guard": payload,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).absolute().parents[1])
    parser.add_argument("--public-workspace", type=Path, default=DEFAULT_PUBLIC_WORKSPACE)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--delete-gl-rows", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--sanitize-loftyassist-reserves", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    root = args.root.expanduser().resolve()
    public_workspace = args.public_workspace.expanduser().resolve()
    report_path = args.report or root / "reports" / "discord_public_financial_source_guard_report.json"
    report = build_report(root, public_workspace, args.delete_gl_rows, args.sanitize_loftyassist_reserves)
    write_json(report_path, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
