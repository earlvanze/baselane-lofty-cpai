#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CONFLICT_COPY_PATTERN = re.compile(r"\bconflicted copy\b", re.IGNORECASE)
SCANNED_REF_ROOTS = ("refs/heads", "refs/remotes", "refs/tags")


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_root() -> Path:
    return Path(os.environ.get("WORKSPACE_ROOT") or Path(__file__).absolute().parents[1])


def git_dir(repo_dir: Path) -> Path:
    direct = repo_dir / ".git"
    if direct.is_dir():
        return direct
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "--git-dir"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise ValueError("not_git_repo")
    resolved = Path(proc.stdout.strip())
    return resolved if resolved.is_absolute() else repo_dir / resolved


def ref_name_is_valid(ref_name: str) -> bool:
    proc = subprocess.run(
        ["git", "check-ref-format", ref_name],
        check=False,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def invalid_loose_refs(repo_dir: Path) -> list[dict[str, Any]]:
    repository_git_dir = git_dir(repo_dir)
    records: list[dict[str, Any]] = []
    for ref_root in SCANNED_REF_ROOTS:
        root = repository_git_dir / ref_root
        if not root.is_dir():
            continue
        for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
            ref_name = path.relative_to(repository_git_dir).as_posix()
            if ref_name_is_valid(ref_name):
                continue
            raw_value = path.read_text(encoding="utf-8", errors="replace").strip()
            object_id = raw_value if re.fullmatch(r"[0-9a-fA-F]{40,64}", raw_value) else None
            records.append(
                {
                    "ref_name": ref_name,
                    "original_path": str(path),
                    "object_id": object_id,
                    "content_sha256": file_sha256(path),
                    "recognized_dropbox_conflict_copy": bool(CONFLICT_COPY_PATTERN.search(path.name)),
                    "action": "pending",
                    "quarantine_path": None,
                }
            )
    return records


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            delete=False,
            dir=path.parent,
            encoding="utf-8",
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as handle:
            tmp_path = Path(handle.name)
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        tmp_path.replace(path)
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()


def run_preflight(
    repo_dir: Path,
    *,
    report_path: Path,
    quarantine_root: Path,
    apply: bool,
) -> dict[str, Any]:
    generated_at = iso_z()
    try:
        records = invalid_loose_refs(repo_dir)
    except (OSError, ValueError) as exc:
        report = {
            "generated_at": generated_at,
            "status": "blocked",
            "reason": str(exc),
            "repo_dir": str(repo_dir),
            "apply": apply,
            "invalid_ref_count": 0,
            "quarantined_count": 0,
            "unresolved_count": 1,
            "records": [],
        }
        write_json(report_path, report)
        return report

    run_quarantine_root = quarantine_root / generated_at.replace("-", "").replace(":", "")
    for record in records:
        if not record["recognized_dropbox_conflict_copy"]:
            record["action"] = "blocked_unrecognized_invalid_ref"
            continue
        if not apply:
            record["action"] = "would_quarantine"
            continue
        source = Path(record["original_path"])
        destination = run_quarantine_root / record["ref_name"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(source), str(destination))
        except OSError as exc:
            record["action"] = "quarantine_failed"
            record["error"] = str(exc)
            continue
        record["action"] = "quarantined"
        record["quarantine_path"] = str(destination)

    quarantined_count = sum(record["action"] == "quarantined" for record in records)
    unresolved_count = sum(record["action"] != "quarantined" for record in records)
    if not records:
        status = "ok"
        reason = "no_invalid_loose_refs"
    elif unresolved_count == 0:
        status = "quarantined"
        reason = "recognized_dropbox_conflict_refs_quarantined"
    elif not apply and all(record["action"] == "would_quarantine" for record in records):
        status = "review"
        reason = "recognized_dropbox_conflict_refs_require_apply"
    else:
        status = "blocked"
        reason = "invalid_git_refs_remain"

    report = {
        "generated_at": generated_at,
        "status": status,
        "reason": reason,
        "repo_dir": str(repo_dir),
        "git_dir": str(git_dir(repo_dir)),
        "apply": apply,
        "invalid_ref_count": len(records),
        "quarantined_count": quarantined_count,
        "unresolved_count": unresolved_count,
        "records": records,
    }
    write_json(report_path, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Quarantine invalid Dropbox conflict-copy loose refs before Assetrail Git operations."
    )
    root = default_root()
    parser.add_argument("--repo-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=root / "reports/baselane_assetrail_git_ref_preflight.json")
    parser.add_argument(
        "--quarantine-root",
        type=Path,
        default=root / "reports/assetrail_git_ref_quarantine",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    report = run_preflight(
        args.repo_dir,
        report_path=args.report,
        quarantine_root=args.quarantine_root,
        apply=args.apply,
    )
    print(json.dumps({"status": report["status"], "reason": report["reason"]}, sort_keys=True))
    return 0 if report["status"] in {"ok", "quarantined"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
