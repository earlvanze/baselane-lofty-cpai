#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Restore a ledger file from a verified baseline backup.")
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--require-current-sha256", required=True)
    parser.add_argument("--report", type=Path, default=Path("reports/restore_ledger_from_verified_baseline.json"))
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    issues: list[str] = []
    if not args.current.is_file():
        issues.append(f"current_missing:{args.current}")
    if not args.baseline.is_file():
        issues.append(f"baseline_missing:{args.baseline}")
    current_sha = sha256_file(args.current) if args.current.is_file() else ""
    baseline_sha = sha256_file(args.baseline) if args.baseline.is_file() else ""
    if current_sha and current_sha != args.require_current_sha256:
        issues.append("current_sha256_mismatch")
    backup_path = args.current.with_name(f"{args.current.name}.pre-restore-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.bak")
    restored = False
    if args.apply and not issues:
        shutil.copy2(args.current, backup_path)
        shutil.copy2(args.baseline, args.current)
        restored = True
    report = {
        "generated_at": iso_z(),
        "status": "restored" if restored else "ready" if not issues else "blocked",
        "apply": args.apply,
        "current": str(args.current),
        "baseline": str(args.baseline),
        "required_current_sha256": args.require_current_sha256,
        "current_sha256_before": current_sha,
        "baseline_sha256": baseline_sha,
        "backup_before_restore": str(backup_path) if restored else None,
        "restored": restored,
        "issue_count": len(issues),
        "issues": issues,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "restored": restored, "issue_count": len(issues)}, indent=2, sort_keys=True))
    return 0 if not issues else 2


if __name__ == "__main__":
    raise SystemExit(main())
