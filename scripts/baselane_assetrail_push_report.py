#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_root() -> Path:
    env_root = os.environ.get("WORKSPACE_ROOT")
    if env_root:
        return Path(env_root)
    return Path(__file__).absolute().parents[1]


def default_dropbox_root(root: Path) -> Path:
    env_root = os.environ.get("DROPBOX_ROOT")
    if env_root:
        return Path(env_root)
    for candidate in (Path("/data/Dropbox"), Path.home() / "Dropbox", Path("/home/digit/Dropbox"), root / "Dropbox"):
        if candidate.is_dir():
            return candidate
    return root / "Dropbox"


def default_ledger_dir(root: Path) -> Path:
    env_dir = os.environ.get("BASELANE_LEDGER_DIR")
    if env_dir:
        return Path(env_dir)
    dropbox_root = default_dropbox_root(root)
    for candidate in (dropbox_root / "Projects" / "assetrail", dropbox_root / "Projects" / "transaction_tracker"):
        if candidate.is_dir():
            return candidate
    return dropbox_root / "Projects" / "assetrail"


def run_git(ledger_dir: Path, *args: str) -> tuple[int, str, str]:
    command = ["git", "-c", f"safe.directory={ledger_dir}", "-C", str(ledger_dir), *args]
    proc = subprocess.run(command, text=True, capture_output=True, check=False)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def file_sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def build_report(root: Path, ledger_dir: Path | None = None) -> dict[str, Any]:
    ledger_dir = ledger_dir or default_ledger_dir(root)
    ledger_path = Path(os.environ.get("BASELANE_LEDGER_PATH") or ledger_dir / "ECO Systems General Ledger.csv")
    report: dict[str, Any] = {
        "generated_at": iso_z(),
        "status": "review",
        "reason": None,
        "ledger_dir": str(ledger_dir),
        "ledger_path": str(ledger_path),
    }
    if not ledger_path.is_file():
        return {**report, "reason": "missing_ledger"}

    try:
        report["ledger_mtime"] = datetime.fromtimestamp(ledger_path.stat().st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")
        report["ledger_size_bytes"] = ledger_path.stat().st_size
        report["ledger_sha256"] = file_sha256(ledger_path)
    except OSError as exc:
        return {**report, "reason": "ledger_stat_failed", "error": str(exc)}

    inside_rc, inside_out, inside_err = run_git(ledger_dir, "rev-parse", "--is-inside-work-tree")
    if inside_rc != 0 or inside_out != "true":
        return {**report, "reason": "not_git_repo", "git_error": inside_err}

    ledger_file = ledger_path.name
    head_rc, git_head, head_err = run_git(ledger_dir, "rev-parse", "--short", "HEAD")
    full_head_rc, git_head_full, full_head_err = run_git(ledger_dir, "rev-parse", "HEAD")
    branch_rc, git_branch, branch_err = run_git(ledger_dir, "rev-parse", "--abbrev-ref", "HEAD")
    upstream_rc, git_upstream, upstream_err = run_git(
        ledger_dir, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"
    )
    upstream_head_rc, git_upstream_head, upstream_head_err = run_git(ledger_dir, "rev-parse", "@{u}")
    ahead_behind_rc, ahead_behind, ahead_behind_err = run_git(
        ledger_dir, "rev-list", "--left-right", "--count", "HEAD...@{u}"
    )
    status_rc, ledger_git_status, status_err = run_git(ledger_dir, "status", "--short", "--", ledger_file)
    timestamp_rc, git_commit_timestamp, timestamp_err = run_git(ledger_dir, "log", "-1", "--format=%cI", "--", ledger_file)
    subject_rc, git_commit_subject, subject_err = run_git(ledger_dir, "log", "-1", "--format=%s", "--", ledger_file)
    ahead_count = None
    behind_count = None
    if ahead_behind_rc == 0 and ahead_behind:
        parts = ahead_behind.split()
        if len(parts) == 2:
            try:
                ahead_count = int(parts[0])
                behind_count = int(parts[1])
            except ValueError:
                pass

    report.update(
        {
            "git_head": git_head if head_rc == 0 else None,
            "git_head_full": git_head_full if full_head_rc == 0 else None,
            "git_branch": git_branch if branch_rc == 0 else None,
            "git_upstream": git_upstream if upstream_rc == 0 else None,
            "git_upstream_head": git_upstream_head if upstream_head_rc == 0 else None,
            "git_upstream_ahead_count": ahead_count,
            "git_upstream_behind_count": behind_count,
            "git_commit_timestamp": git_commit_timestamp if timestamp_rc == 0 else None,
            "git_commit_subject": git_commit_subject if subject_rc == 0 else None,
            "ledger_git_status": ledger_git_status,
        }
    )
    errors = [error for error in (head_err, full_head_err, branch_err, status_err, timestamp_err, subject_err) if error]
    if errors:
        report["git_errors"] = errors
    if status_rc != 0:
        return {**report, "status": "review", "reason": "git_status_failed"}
    if ledger_git_status:
        return {**report, "status": "review", "reason": "ledger_dirty"}
    if not git_head:
        return {**report, "status": "review", "reason": "missing_git_head"}
    if upstream_rc != 0 or not git_upstream:
        report["git_upstream_error"] = upstream_err or "missing_upstream"
        return {**report, "status": "review", "reason": "missing_upstream"}
    if upstream_head_rc != 0 or not git_upstream_head:
        report["git_upstream_error"] = upstream_head_err or "missing_upstream_head"
        return {**report, "status": "review", "reason": "missing_upstream_head"}
    if ahead_behind_rc != 0:
        report["git_upstream_error"] = ahead_behind_err or "ahead_behind_check_failed"
        return {**report, "status": "review", "reason": "git_upstream_status_failed"}
    if git_head_full != git_upstream_head or ahead_count != 0 or behind_count != 0:
        return {**report, "status": "review", "reason": "upstream_not_current"}
    return {**report, "status": "verified_current_clean", "reason": "ok"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Write read-only AssetRail ledger Git evidence for Baselane daily reporting.")
    parser.add_argument("--root", type=Path, default=default_root())
    parser.add_argument("--ledger-dir", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = build_report(args.root, args.ledger_dir)
    report_path = args.report or args.root / "reports" / "baselane_assetrail_push_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = report_path.with_suffix(report_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(report_path)
    print(json.dumps({"status": report["status"], "reason": report.get("reason"), "report": str(report_path)}, sort_keys=True))
    return 0 if report["status"] == "verified_current_clean" else 2


if __name__ == "__main__":
    raise SystemExit(main())
