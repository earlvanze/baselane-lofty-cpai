#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), [dict(row) for row in reader]


def stable_row_key(row: dict[str, str]) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"))


def parse_amount(value: Any) -> float:
    text = str(value or "0").replace(",", "").strip()
    try:
        return float(text)
    except ValueError:
        return 0.0


def aops_kind(row: dict[str, str]) -> str:
    notes = str(row.get("Notes") or "")
    parts = notes.split("|")
    if not parts or not parts[0].startswith("AOPS-"):
        return ""
    if parts[0] == "AOPS-PM-FEE":
        return "pm"
    return parts[1] if len(parts) > 1 else "unknown"


def build_report(current: Path, baseline: Path, restore_script: Path | None = None) -> dict[str, Any]:
    current_headers, current_rows = read_rows(current)
    baseline_headers, baseline_rows = read_rows(baseline)
    current_keys = {stable_row_key(row): row for row in current_rows}
    baseline_keys = {stable_row_key(row): row for row in baseline_rows}
    added = [current_keys[key] for key in sorted(set(current_keys) - set(baseline_keys))]
    removed = [baseline_keys[key] for key in sorted(set(baseline_keys) - set(current_keys))]
    added_aops = [row for row in added if aops_kind(row)]
    added_non_aops = [row for row in added if not aops_kind(row)]
    by_kind: dict[str, int] = {}
    by_kind_amount: dict[str, float] = {}
    for row in added_aops:
        kind = aops_kind(row) or "unknown"
        by_kind[kind] = by_kind.get(kind, 0) + 1
        by_kind_amount[kind] = round(by_kind_amount.get(kind, 0.0) + parse_amount(row.get("Amount")), 2)
    restore_command = ""
    restore_commands_file = ""
    restore_command_safe = False
    restore_command_blockers: list[str] = []
    if restore_script:
        resolved_current = current.resolve()
        resolved_baseline = baseline.resolve()
        if not current.is_file():
            restore_command_blockers.append("current_ledger_missing")
        if not baseline.is_file():
            restore_command_blockers.append("baseline_ledger_missing")
        if str(resolved_current).startswith("/tmp/") or "/pytest-" in str(resolved_current):
            restore_command_blockers.append("current_ledger_path_is_temporary")
        if str(resolved_baseline).startswith("/tmp/") or "/pytest-" in str(resolved_baseline):
            restore_command_blockers.append("baseline_ledger_path_is_temporary")
        restore_command_safe = not restore_command_blockers
        restore_command = " ".join(
            shlex.quote(str(part))
            for part in [
                "python3",
                restore_script,
                "--current",
                current,
                "--baseline",
                baseline,
                "--require-current-sha256",
                sha256_file(current),
                "--apply",
            ]
        )
        restore_commands_file = str(Path("reports/baselane_monthly_accrual_restore_ledger.requires-explicit-approval.sh"))
    safe_to_restore = (
        current_headers == baseline_headers
        and len(added) == len(added_aops)
        and not removed
        and bool(added_aops)
    )
    return {
        "generated_at": iso_z(),
        "status": "restore_ready" if safe_to_restore else "review",
        "current": str(current),
        "baseline": str(baseline),
        "current_sha256": sha256_file(current),
        "baseline_sha256": sha256_file(baseline),
        "headers_match": current_headers == baseline_headers,
        "current_row_count": len(current_rows),
        "baseline_row_count": len(baseline_rows),
        "added_count": len(added),
        "removed_count": len(removed),
        "added_aops_count": len(added_aops),
        "added_non_aops_count": len(added_non_aops),
        "added_aops_amount_sum": round(sum(parse_amount(row.get("Amount")) for row in added_aops), 2),
        "added_aops_count_by_kind": dict(sorted(by_kind.items())),
        "added_aops_amount_by_kind": dict(sorted(by_kind_amount.items())),
        "added_aops_samples": added_aops[:20],
        "added_non_aops_samples": added_non_aops[:20],
        "removed_samples": removed[:20],
        "safe_to_restore_baseline": safe_to_restore,
        "restore_command_safe_to_write": restore_command_safe,
        "restore_command_safety_blockers": restore_command_blockers,
        "restore_command_requires_explicit_operator_execution": restore_command,
        "restore_commands_requires_explicit_operator_execution_file": restore_commands_file,
        "next_action": (
            "Run the restore command only if these AOPS overlay rows were unintended and no other ledger edits should be preserved."
            if safe_to_restore
            else "Review diff before restoring; current differs from baseline by more than appended AOPS overlay rows."
        ),
    }


def write_restore_commands_file(path: Path, report: dict[str, Any]) -> dict[str, Any]:
    restore_command = str(report.get("restore_command_requires_explicit_operator_execution") or "").strip()
    if not restore_command:
        return {"written": False, "path": str(path), "reason": "missing_restore_command"}
    if report.get("restore_command_safe_to_write") is not True:
        return {
            "written": False,
            "path": str(path),
            "reason": "restore_command_safety_blocked",
            "blockers": report.get("restore_command_safety_blockers") or [],
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                "# Requires explicit operator approval after reviewing the accrual append audit.",
                "# Re-audits immediately before restore and refuses if the diff is no longer AOPS-only.",
                'echo "[accrual-restore] re-auditing ledger append diff before restore"',
                (
                    "python3 scripts/audit_accrual_overlay_append_diff.py "
                    f"--current {shlex.quote(str(report['current']))} "
                    f"--baseline {shlex.quote(str(report['baseline']))} "
                    "--report reports/baselane_monthly_accrual_accidental_apply_audit.json "
                    "--restore-script scripts/restore_ledger_from_verified_baseline.py"
                ),
                'STATUS="$(python3 - <<\'PY\'',
                "import json",
                "payload=json.load(open('reports/baselane_monthly_accrual_accidental_apply_audit.json', encoding='utf-8'))",
                "print(payload.get('status') or '')",
                "PY",
                ')"',
                'if [ "$STATUS" != "restore_ready" ]; then',
                '  echo "[accrual-restore] append audit status is $STATUS; refusing restore" >&2',
                "  exit 1",
                "fi",
                'echo "[accrual-restore] audit is restore_ready; restoring verified baseline"',
                restore_command,
                "python3 - <<'PY'",
                "import json",
                "payload=json.load(open('reports/restore_ledger_from_verified_baseline.json', encoding='utf-8'))",
                "if payload.get('status') != 'restored' or payload.get('restored') is not True:",
                "    raise SystemExit('restore report did not confirm restored=true')",
                "print('[accrual-restore] restore confirmed')",
                "PY",
                "",
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return {"written": True, "path": str(path)}


def write_accept_current_decision_scaffold(path: Path, report: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "decision": "accept_current_aops_overlay",
        "reviewed": False,
        "reviewed_at": "",
        "reviewed_by": "",
        "current": report.get("current"),
        "baseline": report.get("baseline"),
        "current_sha256": report.get("current_sha256"),
        "baseline_sha256": report.get("baseline_sha256"),
        "added_aops_count": report.get("added_aops_count"),
        "added_non_aops_count": report.get("added_non_aops_count"),
        "removed_count": report.get("removed_count"),
        "added_aops_amount_sum": report.get("added_aops_amount_sum"),
        "instructions": (
            "Set reviewed=true, reviewed_at, and reviewed_by only after deciding to keep the current AOPS overlay rows "
            "instead of restoring the verified baseline. Any hash/count drift invalidates this decision."
        ),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return {"written": True, "path": str(path)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit current ledger rows added after a baseline, focused on AOPS accrual overlay appends.")
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--restore-script", type=Path)
    parser.add_argument("--restore-commands-file", type=Path, default=Path("reports/baselane_monthly_accrual_restore_ledger.requires-explicit-approval.sh"))
    parser.add_argument("--accept-current-decision-scaffold", type=Path, default=Path("config/baselane_monthly_accrual_append_audit_decision.json"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_report(args.current, args.baseline, args.restore_script)
    restore_commands = write_restore_commands_file(args.restore_commands_file, report) if args.restore_script else {"written": False}
    report["restore_commands_requires_explicit_operator_execution"] = restore_commands
    report["accept_current_decision_scaffold"] = write_accept_current_decision_scaffold(
        args.accept_current_decision_scaffold,
        report,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "added_count", "added_aops_count", "removed_count", "safe_to_restore_baseline")}, indent=2, sort_keys=True))
    return 0 if report["status"] == "restore_ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
