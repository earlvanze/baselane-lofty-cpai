#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
DEFAULT_REPORT = REPORTS / "lofty_owner_update_draft_hygiene.json"
UNSAFE_SECTION_RE = re.compile(
    r"(?mi)^##\s+(?:Monthly send checklist|Monthly review checklist|Internal context\b|Internal operations context\b)"
)


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_real_estate_root() -> Path:
    env_root = os.environ.get("REAL_ESTATE_ROOT")
    if env_root:
        return Path(env_root)
    for candidate in (
        Path("/mnt/c/Users/digit/Dropbox/Real Estate"),
        Path("/home/digit/Dropbox/Real Estate"),
    ):
        if candidate.exists():
            return candidate
    return Path("/mnt/c/Users/digit/Dropbox/Real Estate")


def sidecar_path_for(draft_path: Path) -> Path:
    name = draft_path.name
    if name.endswith("-draft.md"):
        return draft_path.with_name(name[: -len("-draft.md")] + "-review-checklist.md")
    return draft_path.with_name(draft_path.stem + "-review-checklist.md")


def split_draft(text: str) -> tuple[str, str] | None:
    match = UNSAFE_SECTION_RE.search(text)
    if not match:
        return None
    public_text = text[: match.start()].rstrip() + "\n"
    review_text = text[match.start() :].strip() + "\n"
    if not review_text.strip():
        return None
    return public_text, review_text


def is_archive_path(path: Path) -> bool:
    return any(part in {"_Archive", ".archive", "archive"} for part in path.parts)


def scan_drafts(root: Path, include_archive: bool, month: str | None, canonical_snapshot_only: bool) -> list[Path]:
    if not root.exists():
        return []
    drafts: list[Path] = []
    for draft_path in root.rglob("*owner-update-*-draft.md"):
        if not include_archive and is_archive_path(draft_path):
            continue
        if month and not draft_path.name.startswith(f"{month}-"):
            continue
        if canonical_snapshot_only and "00 - README & Property Snapshot" not in draft_path.parts:
            continue
        drafts.append(draft_path)
    return sorted(drafts)


def evaluate_draft(draft_path: Path) -> dict[str, Any]:
    try:
        text = draft_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return {
            "path": str(draft_path),
            "status": "blocked",
            "reason": "unreadable",
            "error": str(exc),
        }
    split = split_draft(text)
    if not split:
        return {
            "path": str(draft_path),
            "status": "clean",
            "sidecar_path": str(sidecar_path_for(draft_path)),
        }
    public_text, review_text = split
    sidecar_path = sidecar_path_for(draft_path)
    sidecar_exists = sidecar_path.exists()
    sidecar_matches = False
    if sidecar_exists:
        try:
            sidecar_matches = sidecar_path.read_text(encoding="utf-8", errors="replace") == review_text
        except Exception:
            sidecar_matches = False
    return {
        "path": str(draft_path),
        "status": "needs_split" if not sidecar_exists or sidecar_matches else "blocked",
        "reason": None if not sidecar_exists or sidecar_matches else "sidecar_exists_with_different_content",
        "sidecar_path": str(sidecar_path),
        "sidecar_exists": sidecar_exists,
        "sidecar_matches": sidecar_matches,
        "public_char_count": len(public_text),
        "review_char_count": len(review_text),
    }


def apply_split(draft_path: Path, report_entry: dict[str, Any]) -> bool:
    if report_entry.get("status") != "needs_split":
        return False
    text = draft_path.read_text(encoding="utf-8", errors="replace")
    split = split_draft(text)
    if not split:
        return False
    public_text, review_text = split
    sidecar_path = Path(str(report_entry["sidecar_path"]))
    if sidecar_path.exists() and sidecar_path.read_text(encoding="utf-8", errors="replace") != review_text:
        report_entry["status"] = "blocked"
        report_entry["reason"] = "sidecar_exists_with_different_content"
        return False
    backup_path = draft_path.with_name(f"{draft_path.name}.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(draft_path, backup_path)
    sidecar_path.write_text(review_text, encoding="utf-8")
    draft_path.write_text(public_text, encoding="utf-8")
    report_entry["status"] = "split_applied"
    report_entry["backup_path"] = str(backup_path)
    return True


def build_report(
    root: Path,
    report_path: Path,
    apply: bool,
    include_archive: bool,
    month: str | None = None,
    canonical_snapshot_only: bool = False,
) -> dict[str, Any]:
    entries = [
        evaluate_draft(draft_path)
        for draft_path in scan_drafts(root, include_archive, month, canonical_snapshot_only)
    ]
    applied_count = 0
    if apply:
        for entry in entries:
            if entry.get("status") == "needs_split" and apply_split(Path(str(entry["path"])), entry):
                applied_count += 1
    needs_split = [entry for entry in entries if entry.get("status") == "needs_split"]
    blocked = [entry for entry in entries if entry.get("status") == "blocked"]
    report = {
        "generated_at": iso_z(),
        "status": "review" if needs_split or blocked else "ok",
        "root": str(root),
        "apply": apply,
        "include_archive": include_archive,
        "month": month,
        "canonical_snapshot_only": canonical_snapshot_only,
        "draft_count": len(entries),
        "needs_split_count": len(needs_split),
        "blocked_count": len(blocked),
        "applied_count": applied_count,
        "entries_bounded": entries[:200],
    }
    report["issue_count"] = report["needs_split_count"] + report["blocked_count"]
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Split internal monthly owner-update review content out of owner-facing draft files.")
    parser.add_argument("--root", type=Path, default=default_real_estate_root())
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--include-archive", action="store_true")
    parser.add_argument("--month", help="Limit to owner update draft filenames starting with YYYY-MM.")
    parser.add_argument("--canonical-snapshot-only", action="store_true", help="Limit to Public/00 - README & Property Snapshot drafts.")
    args = parser.parse_args(argv)
    report = build_report(args.root, args.report, args.apply, args.include_archive, args.month, args.canonical_snapshot_only)
    print(json.dumps({key: report[key] for key in ("status", "issue_count", "needs_split_count", "blocked_count", "applied_count")}, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" or not args.apply else 2


if __name__ == "__main__":
    raise SystemExit(main())
