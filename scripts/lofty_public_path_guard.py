#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path


FORBIDDEN_PATTERNS = [
    re.compile(r"Public/Financials"),
    re.compile(r"Public/Updates"),
    re.compile(r"/home/digit/\.openclaw/workspace/Dropbox"),
    re.compile(r"Public['\"]?\s*,\s*['\"]Financials"),
    re.compile(r"Public['\"]?\s*,\s*['\"]Updates"),
    re.compile(r"/\s*['\"]Financials['\"]"),
    re.compile(r"/\s*['\"]Updates['\"]"),
    re.compile(r"join\([^)]*['\"]Financials['\"]"),
    re.compile(r"join\([^)]*['\"]Updates['\"]"),
    re.compile(r"\bFinancials folder\b"),
    re.compile(r"\bUpdates folder\b"),
    re.compile(r"parent\.name\s*==\s*['\"]Financials['\"]"),
    re.compile(r"parent\.name\s*==\s*['\"]Updates['\"]"),
]

DEFAULT_IGNORES = [
    re.compile(r"/__pycache__/"),
    re.compile(r"\.bak($|[-._])"),
    re.compile(r"/scripts/pdf-tools/(debug|test|inspect)"),
    re.compile(r"/scripts/baselane_scope_guard\.py$"),
]
PRUNE_DIR_NAMES = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tmp", "tmp", ".cache"}
SCANNED_SUFFIXES = {"", ".py", ".js", ".mjs", ".sh", ".md", ".json", ".csv"}
MAX_SCAN_BYTES = 1_000_000
SCRIPT_SELF = Path(__file__).absolute()
DEFAULT_ARTIFACT_RELATIVE_PATHS = [
    "reports/baselane_monthly_owner_review_gate.csv",
    "reports/baselane_monthly_owner_review_gate.md",
    "reports/baselane_monthly_owner_review_gate.json",
    "reports/baselane_financials_operations_packet.json",
    "reports/baselane_financials_operations_packet.md",
    "reports/baselane_financials_monthly_review_manifest.json",
    "reports/baselane_financials_monthly_review_candidate_packet.json",
    "reports/baselane_financials_monthly_run_report.json",
    "reports/baselane_financials_monthly_readiness.json",
    "reports/baselane_financials_monthly_lofty_pm_runtime_map.json",
    "reports/baselane_financials_monthly_lofty_pm_publish.json",
    "skills/lofty-pm/config/property_update_map.json",
]


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_root() -> Path:
    env_root = os.environ.get("OPENCLAW_WORKSPACE") or os.environ.get("WORKSPACE_ROOT")
    if env_root:
        return Path(env_root)
    cwd = Path.cwd()
    if (cwd / "scripts").is_dir():
        return cwd
    return Path(__file__).absolute().parents[1]


def default_scan_roots(root: Path) -> list[Path]:
    openclaw_root = Path(os.environ.get("OPENCLAW_ROOT") or root.parent)
    candidates = [
        root / "scripts",
        root / "skills" / "baselane-financials" / "scripts",
        root / "skills" / "lofty-pm" / "scripts",
        openclaw_root / "workspace-lofty-vp-comms" / "scripts",
    ]
    return [path for path in candidates if path.is_dir()]


def default_artifact_paths(root: Path) -> list[Path]:
    return [root / relative for relative in DEFAULT_ARTIFACT_RELATIVE_PATHS if (root / relative).is_file()]


def should_ignore(path: Path, root: Path, ignores: list[re.Pattern[str]]) -> bool:
    if path.absolute() == SCRIPT_SELF or path.resolve() == SCRIPT_SELF.resolve():
        return True
    text = "/" + str(path).replace("\\", "/")
    try:
        rel = "/" + str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        rel = text
    return any(pattern.search(text) or pattern.search(rel) for pattern in ignores)


def scan_file(path: Path) -> list[dict]:
    issues = []
    try:
        handle = path.open("r", encoding="utf-8", errors="ignore")
    except Exception as exc:  # noqa: BLE001
        return [{"file": str(path), "line": None, "pattern": "unreadable", "text": str(exc)}]
    with handle:
        for line_number, line in enumerate(handle, start=1):
            for pattern in FORBIDDEN_PATTERNS:
                if pattern.search(line):
                    issues.append(
                        {
                            "file": str(path),
                            "line": line_number,
                            "pattern": pattern.pattern,
                            "text": line.strip()[:240],
                        }
                    )
    return issues


def scan_path(path: Path, root: Path, ignores: list[re.Pattern[str]]) -> tuple[list[dict], bool, bool]:
    if should_ignore(path, root, ignores):
        return [], False, False
    if path.suffix.lower() not in SCANNED_SUFFIXES:
        return [], False, False
    try:
        size = path.stat().st_size
    except OSError:
        return [], False, False
    if size > MAX_SCAN_BYTES:
        return [], False, True
    return scan_file(path), True, False


def iter_json_records(value: object) -> list[dict]:
    records: list[dict] = []
    if isinstance(value, dict):
        records.append(value)
        for child in value.values():
            records.extend(iter_json_records(child))
    elif isinstance(value, list):
        for child in value:
            records.extend(iter_json_records(child))
    return records


def canonical_public_file_issue(path_text: str, artifact_path: Path) -> dict | None:
    if not path_text or "/Public/" not in path_text.replace("\\", "/"):
        return None
    path = Path(path_text).expanduser()
    if path.is_file():
        return None
    normalized = path_text.replace("\\", "/")
    prefix, suffix = normalized.split("/Public/", 1)
    property_path = Path(prefix)
    if not property_path.is_dir():
        return None
    conflict_matches = []
    for sibling in property_path.iterdir():
        if not sibling.is_dir():
            continue
        if not sibling.name.startswith("Public ("):
            continue
        conflict_file = sibling / suffix
        if conflict_file.is_file():
            conflict_matches.append(str(conflict_file))
    if not conflict_matches:
        return None
    return {
        "file": str(artifact_path),
        "line": None,
        "pattern": "missing_canonical_public_with_conflicted_copy",
        "text": f"canonical Public target missing but conflicted Public copy has file: {path_text}",
        "target_path": path_text,
        "conflicted_copy_matches": sorted(conflict_matches),
    }


def scan_public_runtime_artifact(path: Path) -> tuple[list[dict], int]:
    if path.suffix.lower() != ".json" or not path.is_file():
        return [], 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return [], 0
    issues: list[dict] = []
    checked = 0
    seen: set[str] = set()
    for record in iter_json_records(data):
        for key in ("updates_md", "financials_md", "description_md", "draft_path"):
            value = record.get(key)
            if not isinstance(value, str) or value in seen:
                continue
            seen.add(value)
            checked += 1
            issue = canonical_public_file_issue(value, path)
            if issue:
                issues.append(issue)
    return issues, checked


def build_report(
    root: Path,
    scan_roots: list[Path],
    artifact_paths: list[Path] | None = None,
    *,
    check_artifact_public_targets: bool = True,
) -> dict:
    ignores = DEFAULT_IGNORES
    files_scanned = 0
    skipped_large_count = 0
    issues = []
    artifact_paths = artifact_paths if artifact_paths is not None else default_artifact_paths(root)
    for scan_root in scan_roots:
        for current_root, dirs, files in os.walk(scan_root):
            dirs[:] = sorted(name for name in dirs if name not in PRUNE_DIR_NAMES)
            for name in sorted(files):
                path = Path(current_root) / name
                path_issues, scanned, skipped_large = scan_path(path, root, ignores)
                if skipped_large:
                    skipped_large_count += 1
                if scanned:
                    files_scanned += 1
                    issues.extend(path_issues)
    artifact_files_scanned = 0
    artifact_skipped_large_count = 0
    artifact_public_targets_checked = 0
    seen_artifacts = set()
    for artifact_path in sorted(artifact_paths):
        path = artifact_path.expanduser().resolve()
        if path in seen_artifacts or not path.is_file():
            continue
        seen_artifacts.add(path)
        path_issues, scanned, skipped_large = scan_path(path, root, ignores)
        if skipped_large:
            artifact_skipped_large_count += 1
            skipped_large_count += 1
        if scanned:
            artifact_files_scanned += 1
            files_scanned += 1
            issues.extend(path_issues)
        if check_artifact_public_targets:
            public_issues, public_checked = scan_public_runtime_artifact(path)
            artifact_public_targets_checked += public_checked
            issues.extend(public_issues)
    return {
        "generated_at": iso_z(),
        "status": "ok" if not issues else "review",
        "issue_count": len(issues),
        "files_scanned": files_scanned,
        "artifact_files_scanned": artifact_files_scanned,
        "artifact_public_targets_checked": artifact_public_targets_checked,
        "artifact_public_target_check_enabled": check_artifact_public_targets,
        "artifact_skipped_large_count": artifact_skipped_large_count,
        "artifact_paths": [str(path) for path in sorted(seen_artifacts)],
        "skipped_large_count": skipped_large_count,
        "scan_roots": [str(path) for path in scan_roots],
        "forbidden_policy": "Active scripts and monthly artifacts must not write or target legacy Financials/Updates folders or workspace-mirrored Dropbox roots; canonical guarded markdown destinations are Public/00 - README & Property Snapshot under the real Dropbox source tree. Bank statements, ledgers, and workbook artifacts remain under Public/07 - P&L & Owner Statements.",
        "canonical_financials_folder": "Public/00 - README & Property Snapshot",
        "canonical_updates_folder": "Public/00 - README & Property Snapshot",
        "issues": issues,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Detect active script references to legacy Public/Financials and Public/Updates destinations.")
    parser.add_argument("--root", default=str(default_root()))
    parser.add_argument("--report", default="")
    parser.add_argument("--scan-root", action="append", default=[])
    parser.add_argument("--artifact-path", action="append", default=[])
    parser.add_argument("--no-default-artifacts", action="store_true")
    parser.add_argument(
        "--skip-artifact-public-target-checks",
        action="store_true",
        help="Skip slow Dropbox-backed artifact target existence checks; still scans script and artifact text for forbidden paths.",
    )
    args = parser.parse_args()

    root = Path(args.root).expanduser().resolve()
    scan_roots = [Path(value).expanduser().resolve() for value in args.scan_root] if args.scan_root else default_scan_roots(root)
    if args.artifact_path:
        artifact_paths = [Path(value).expanduser().resolve() for value in args.artifact_path]
    elif args.no_default_artifacts:
        artifact_paths = []
    else:
        artifact_paths = default_artifact_paths(root)
    report = build_report(
        root,
        scan_roots,
        artifact_paths,
        check_artifact_public_targets=not args.skip_artifact_public_target_checks,
    )
    report_path = Path(args.report).expanduser() if args.report else root / "reports" / "lofty_public_path_guard_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "issue_count", "files_scanned")}, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
