#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


STAGING_MARKERS = (
    "/mnt/f/.openclaw",
    "/mnt/f/OpenClaw",
)
WORKSPACE_DROPBOX_MARKERS = (
    "/data/.openclaw/workspace/Dropbox",
    "/home/digit/.openclaw/workspace/Dropbox",
    "/home/umbrel/.openclaw/workspace/Dropbox",
)


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def is_staging_path(path: Path) -> bool:
    text = str(path.expanduser())
    return any(text == marker or text.startswith(f"{marker}/") for marker in STAGING_MARKERS)


def is_workspace_dropbox_path(path: Path) -> bool:
    text = str(path.expanduser())
    return any(text == marker or text.startswith(f"{marker}/") for marker in WORKSPACE_DROPBOX_MARKERS)


def parse_path(value: str) -> tuple[str, Path]:
    if "=" in value:
        label, path = value.split("=", 1)
        return label.strip() or "path", Path(path)
    return "path", Path(value)


def build_report(paths: list[tuple[str, Path]], allow_staging: bool) -> dict:
    issues = []
    records = []
    for label, path in paths:
        staging = is_staging_path(path)
        workspace_dropbox = is_workspace_dropbox_path(path)
        record = {
            "label": label,
            "path": str(path),
            "staging_path": staging,
            "workspace_dropbox_path": workspace_dropbox,
            "status": "ok",
        }
        if staging and not allow_staging:
            record["status"] = "blocked"
            issues.append(
                {
                    "issue": "staging_path_not_allowed",
                    "label": label,
                    "path": str(path),
                    "detail": "Baselane/Lofty PM automation must not read from or write to /mnt/f staging unless explicitly allowed.",
                }
            )
        if workspace_dropbox:
            record["status"] = "blocked"
            issues.append(
                {
                    "issue": "workspace_dropbox_not_allowed",
                    "label": label,
                    "path": str(path),
                    "detail": "Baselane/Lofty PM automation must use Dropbox-sourced folders, not OpenClaw workspace-mirrored Dropbox paths.",
                }
            )
        records.append(record)
    return {
        "generated_at": iso_z(),
        "status": "ok" if not issues else "blocked",
        "allow_staging": allow_staging,
        "issue_count": len(issues),
        "issues": issues,
        "records": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail closed when Baselane/Lofty automation is pointed at staging paths.")
    parser.add_argument("--path", action="append", default=[], help="Path to validate, optionally label=/path")
    parser.add_argument("--allow-staging", action="store_true", help="Explicitly allow /mnt/f staging paths")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    report = build_report([parse_path(value) for value in args.path], args.allow_staging)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
