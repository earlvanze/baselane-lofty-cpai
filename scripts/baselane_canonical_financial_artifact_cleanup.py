#!/usr/bin/env python3
"""Canonicalize current Dropbox financial work products without guessing.

The cleanup is dry-run by default. Applied runs use the desktop trash when
available and record content hashes for every replacement.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable


OWNER_STATEMENTS = "07 - P&L & Owner Statements"
CANONICAL_FINANCIALS_SUFFIX = ("Public", "00 - README & Property Snapshot", "FINANCIALS.md")
SKIP_DIR_NAMES = {".dropbox.cache", "node_modules", "__pycache__", ".git"}
HEAVY_NONFINANCIAL_DIR_RE = re.compile(
    r"(?i)^(?:photos?(?:\s*&\s*video)?|videos?|images?|virtual tours?|bank statements?|tenant ledgers?|receipts?|inspection photos?)$"
)
BACKUP_RE = re.compile(
    r"(?i)(conflicted copy|\bconflict\b|\.bak(?:\.|$)|\bbackup\b|\.before-|\bpre[-_ ]?repair\b|\.tmp(?:\.|$)|~$)"
)
DERIVATIVE_LEDGER_RE = re.compile(r"(?i)(\.filtered(?:\.|$)|\.bak(?:\.|$)|\.tmp(?:\.|$)|\bconflict|pre[-_ ]?repair)")
YEAR_RE = re.compile(r"(?<!\d)(20\d{2})(?!\d)")


@dataclass(frozen=True)
class PlannedAction:
    kind: str
    reason: str
    source: str
    target: str | None = None


@dataclass
class ActionResult:
    kind: str
    reason: str
    source: str
    target: str | None
    source_sha256_before: str | None
    target_sha256_before: str | None
    target_sha256_after: str | None
    method: str
    status: str
    error: str | None = None


# These choices were inspected against the current source ledger and workbook
# schemas. A replacement means the source content wins at the target path.
EXPLICIT_REPLACEMENTS: tuple[tuple[str, str, str], ...] = (
    (
        "OH/Ohio 3-Property Package/Public/07 - P&L & Owner Statements/Statements/Cash Flow Statement - 1258 Lily St.xlsx",
        "OH/Ohio 3-Property Package/Public/07 - P&L & Owner Statements/Cash Flow Statement - 1258 Lily St.xlsx",
        "move inspected child workbook into canonical owner-statements directory",
    ),
    (
        "OH/Ohio 3-Property Package/Public/07 - P&L & Owner Statements/Statements/Cash Flow Statement - 1321 Allendale Ave.xlsx",
        "OH/Ohio 3-Property Package/Public/07 - P&L & Owner Statements/Cash Flow Statement - 1321 Allendale Ave.xlsx",
        "move inspected child workbook into canonical owner-statements directory",
    ),
    (
        "OH/Ohio 3-Property Package/Public/07 - P&L & Owner Statements/Statements/Cash Flow Statement - 1518 Dille Rd.xlsx",
        "OH/Ohio 3-Property Package/Public/07 - P&L & Owner Statements/Cash Flow Statement - 1518 Dille Rd.xlsx",
        "move inspected child workbook into canonical owner-statements directory",
    ),
    (
        "OH/2094 W 34th Place, Cleveland, OH 44113/Public/07 - P&L & Owner Statements/Cash Flow Statement - 2094 - 2094 W 34th Pl, Cleveland, OH 44113.xlsx",
        "OH/2094 W 34th Place, Cleveland, OH 44113/Public/07 - P&L & Owner Statements/Cash Flow Statement - 2094 W 34th Place.xlsx",
        "replace canonical filename with inspected complete workbook",
    ),
    (
        "CO/326-332 S Alcott St Denver, CO 80219/Public/07 - P&L & Owner Statements/ECO Systems General Ledger - 326 South Alcott Street.csv",
        "CO/326-332 S Alcott St Public/07 - P&L & Owner Statements/ECO Systems General Ledger - 326 South Alcott Street.csv",
        "move current 302-row ledger into canonical Public property root",
    ),
    (
        "CO/326-332 S Alcott St Public/07 - P&L & Owner Statements/Cash Flow Statement - 326-332 S Alcott St Denver, CO 80219.xlsx",
        "CO/326-332 S Alcott St Public/07 - P&L & Owner Statements/Cash Flow Statement - 326 South Alcott Street.xlsx",
        "align inspected workbook with deterministic property filename",
    ),
    (
        "OH/16713 Lotus Dr, Cleveland, OH 44128/Public/07 - P&L & Owner Statements/Statements/Cash Flow Statement - 16713 Lotus Drive.xlsx",
        "OH/16713 Lotus Dr, Cleveland, OH 44128/Public/07 - P&L & Owner Statements/Cash Flow Statement - 16713 Lotus Drive.xlsx",
        "replace canonical filename with inspected complete workbook",
    ),
    (
        "OH/3905 E 189th St Cleveland, OH 44122/Public/07 - P&L & Owner Statements/Statements/Cash Flow Statement - 3905 E 189th St.xlsx",
        "OH/3905 E 189th St Cleveland, OH 44122/Public/07 - P&L & Owner Statements/Cash Flow Statement - 3905 E 189th St.xlsx",
        "replace canonical filename with inspected complete workbook",
    ),
)


EXPLICIT_TRASH: tuple[tuple[str, str], ...] = (
    ("OH/Ohio 3-Property Package/Public/07 - P&L & Owner Statements/Statements/Cash Flow Statement - 1258 Lily St - from non-shared Public 1.xlsx", "redundant inspected child workbook"),
    ("OH/Ohio 3-Property Package/Public/07 - P&L & Owner Statements/Statements/Cash Flow Statement - 1321 Allendale Ave - Ohio 3 Property Package, Akron, Ohio 44117.xlsx", "redundant inspected child workbook"),
    ("OH/Ohio 3-Property Package/Public/1518 Dille Road, Euclid, OH 44117/Public/07 - P&L & Owner Statements/Cash Flow Statement - 1518 Dille Rd - Ohio 3 Property Package, Akron, Ohio 44117.xlsx", "redundant inspected child workbook"),
    ("CA/22 W Main Street, Ione, CA 95640/Public/07 - P&L & Owner Statements/Statements/Cash Flow Statement - 22 W Main St - 22 W Main Street, Ione, CA 95640.xlsx", "redundant inspected cash-flow workbook"),
    ("CA/22 W Main Street, Ione, CA 95640/Public/07 - P&L & Owner Statements/Statements/Cash Flow Statement - 22 W Main St.xlsx", "redundant inspected cash-flow workbook"),
    ("IL/25 Circle Dr, Dixmoor, IL 60426/Public/07 - P&L & Owner Statements/Cash Flow Statement - 25 Circle Dr.xlsx", "redundant inspected cash-flow workbook"),
    ("CO/326-332 S Alcott St Denver, CO 80219/Public/07 - P&L & Owner Statements/Cash Flow Statement - 326 South Alcott Street.xlsx", "noncanonical property-root cash-flow workbook"),
    ("OH/3493 W 119th St, Cleveland, Ohio 44111/Public/07 - P&L & Owner Statements/Cash Flow Statement - 3493 West 119th St - 3493 W 119th St, Cleveland, Ohio 44111.xlsx", "redundant inspected cash-flow workbook"),
    ("NY/84 Madison Ave Albany, NY 12202/07 - P&L & Owner Statements/Cash Flow Statement - 84 Madison Ave Albany, NY 12202.xlsx", "noncanonical Madison cash-flow workbook"),
    ("NY/84 Madison Ave Albany, NY 12202/07 - P&L & Owner Statements/ECO Systems General Ledger - 84 Madison Ave.csv", "noncanonical Madison general ledger"),
    ("NY/86 Madison Ave Albany, NY 12202/07 - P&L & Owner Statements/Cash Flow Statement - 86 Madison Ave Albany, NY 12202.xlsx", "noncanonical Madison cash-flow workbook"),
    ("NY/86 Madison Ave Albany, NY 12202/07 - P&L & Owner Statements/ECO Systems General Ledger - 86 Madison Ave.csv", "noncanonical Madison general ledger"),
    ("NY/88 Madison Ave Albany, NY 12202/07 - P&L & Owner Statements/Cash Flow Statement - 88 Madison Ave Albany, NY 12202.xlsx", "noncanonical Madison cash-flow workbook"),
    ("NY/88 Madison Ave Albany, NY 12202/07 - P&L & Owner Statements/ECO Systems General Ledger - 88 Madison Ave.csv", "noncanonical Madison general ledger"),
    ("NY/90 Madison Ave Albany, NY 12202/Public/07 - P&L & Owner Statements/Cash Flow Statement - 90 Madison Ave.xlsx", "noncanonical Madison cash-flow workbook"),
    ("OH/10917 Fidelity Ave, Cleveland, OH 44111/Public/07 - P&L & Owner Statements/P&L Statements/Cash Flow Statement - 10917 Fidelity Ave.xlsx", "redundant inspected cash-flow workbook"),
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_pre_2026_historical(path: Path) -> bool:
    years = [int(value) for value in YEAR_RE.findall(str(path))]
    return bool(years) and max(years) <= 2025


def is_under(path: Path, root: Path) -> bool:
    try:
        Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(root)))
        return True
    except ValueError:
        return False


def walk_files(root: Path, *, max_depth: int) -> Iterable[Path]:
    if not root.exists():
        return
    for current, dirs, files in os.walk(root):
        current_path = Path(current)
        depth = len(current_path.relative_to(root).parts)
        if depth >= max_depth:
            dirs[:] = []
        else:
            dirs[:] = [
                name
                for name in dirs
                if name not in SKIP_DIR_NAMES and not HEAVY_NONFINANCIAL_DIR_RE.match(name)
            ]
        for name in files:
            yield current_path / name


def financial_scan_roots(root: Path) -> list[Path]:
    """Find shallow Public and owner-statement roots without walking media trees."""
    discovered: list[Path] = []
    for current, dirs, _files in os.walk(root):
        current_path = Path(current)
        depth = len(current_path.relative_to(root).parts)
        if depth >= 3:
            dirs[:] = []
            continue
        dirs[:] = [name for name in dirs if name not in SKIP_DIR_NAMES]
        for name in dirs:
            candidate = current_path / name
            if name == "Public" or name.endswith(" Public") or name == OWNER_STATEMENTS:
                discovered.append(candidate)

    selected: list[Path] = []
    for candidate in sorted(set(discovered), key=lambda path: (len(path.parts), str(path))):
        if any(is_under(candidate, parent) for parent in selected):
            continue
        if "AR/1 Coolwood Dr" in str(candidate.relative_to(root)).replace("\\", "/"):
            continue
        selected.append(candidate)
    return selected


def is_public_file(path: Path) -> bool:
    return "Public" in path.parts or any(part.endswith(" Public") for part in path.parts)


def is_canonical_financials_md(path: Path) -> bool:
    if len(path.parts) < 3:
        return False
    public_part, directory, filename = path.parts[-3:]
    return (
        (public_part == "Public" or public_part.endswith(" Public"))
        and directory == CANONICAL_FINANCIALS_SUFFIX[1]
        and filename == CANONICAL_FINANCIALS_SUFFIX[2]
    )


def add_action(actions: dict[str, PlannedAction], action: PlannedAction) -> None:
    existing = actions.get(action.source)
    if existing is None or (existing.kind == "trash" and action.kind == "replace"):
        actions[action.source] = action


def actions_from_stale_report(root: Path, report: Path | None) -> list[PlannedAction]:
    if report is None or not report.is_file():
        return []
    payload = json.loads(report.read_text(encoding="utf-8"))
    result = []
    for entry in payload.get("issues", []):
        path = Path(str(entry.get("detail", "")))
        if path.is_file() and is_under(path, root):
            result.append(PlannedAction("trash", str(entry.get("code", "stale financial artifact")), str(path)))
    return result


def build_plan(root: Path, stale_report: Path | None, global_ledger_root: Path | None) -> tuple[list[PlannedAction], list[dict]]:
    root = root.resolve(strict=False)
    actions: dict[str, PlannedAction] = {}
    reviews: list[dict] = []

    for source_rel, target_rel, reason in EXPLICIT_REPLACEMENTS:
        source = root / source_rel
        target = root / target_rel
        if source.is_file():
            add_action(actions, PlannedAction("replace", reason, str(source), str(target)))

    for source_rel, reason in EXPLICIT_TRASH:
        source = root / source_rel
        if source.is_file():
            add_action(actions, PlannedAction("trash", reason, str(source)))

    for action in actions_from_stale_report(root, stale_report):
        add_action(actions, action)

    for scan_root in financial_scan_roots(root):
        max_depth = 3 if scan_root.name == OWNER_STATEMENTS else 2
        for path in walk_files(scan_root, max_depth=max_depth):
            relative_text = str(path.relative_to(root))
            lower_name = path.name.lower()
            historical = is_pre_2026_historical(path)

            if path.suffix.lower() == ".md" and is_public_file(path):
                if BACKUP_RE.search(path.name) or path.name.startswith("._"):
                    add_action(actions, PlannedAction("trash", "conflict or backup Markdown", str(path)))
                elif "financial" in lower_name and not is_canonical_financials_md(path) and not historical:
                    add_action(actions, PlannedAction("trash", "noncanonical current financial Markdown", str(path)))
                continue

            if path.suffix.lower() not in {".csv", ".xlsx"}:
                continue
            is_gl = lower_name.startswith("eco systems general ledger")
            is_cf = lower_name.startswith("cash flow statement")
            if not (is_gl or is_cf):
                continue
            if "transactions (" in lower_name or historical:
                continue
            if BACKUP_RE.search(path.name) or path.name.startswith("._"):
                add_action(actions, PlannedAction("trash", "conflict or backup financial artifact", str(path)))
                continue
            normalized = relative_text.replace("\\", "/")
            if "Archived Cash Flow Statement Outputs/2026-" in normalized or "Archived ECO Systems General Ledger Outputs/2026-" in normalized:
                add_action(actions, PlannedAction("trash", "current-year archived duplicate financial artifact", str(path)))

    if global_ledger_root and global_ledger_root.exists():
        canonical = global_ledger_root / "ECO Systems General Ledger.csv"
        for path in global_ledger_root.iterdir():
            if not path.is_file() or path == canonical:
                continue
            if not path.name.lower().startswith("eco systems general ledger"):
                continue
            if is_pre_2026_historical(path):
                continue
            # This directory is the global source-export work area. Only the
            # exact canonical filename is an active source; all derivatives are
            # recoverable backups in Dropbox trash.
            reason = "noncanonical global ledger derivative"
            if DERIVATIVE_LEDGER_RE.search(path.name):
                reason = "global ledger backup, conflict, filtered, or temporary derivative"
            add_action(actions, PlannedAction("trash", reason, str(path)))
        if not canonical.is_file():
            reviews.append({"code": "canonical_global_ledger_missing", "detail": str(canonical)})

    replacements = sorted((action for action in actions.values() if action.kind == "replace"), key=lambda item: item.source)
    trash = sorted((action for action in actions.values() if action.kind == "trash"), key=lambda item: item.source)
    return replacements + trash, reviews


TrashCallable = Callable[[Path], str]


def recoverable_trash(path: Path) -> str:
    if not path.exists():
        return "already_absent"
    completed = subprocess.run(
        ["gio", "trash", "-f", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode == 0 and not path.exists():
        return "gio_trash"
    path.unlink()
    return "unlink_fallback"


def atomic_copy(source: Path, target: Path, trasher: TrashCallable) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    os.close(descriptor)
    temp_path = Path(temp_name)
    try:
        shutil.copy2(source, temp_path)
        with temp_path.open("rb") as handle:
            os.fsync(handle.fileno())
        target_method = "target_absent"
        if target.exists():
            target_method = trasher(target)
        os.replace(temp_path, target)
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return target_method


def execute_action(action: PlannedAction, apply: bool, trasher: TrashCallable = recoverable_trash) -> ActionResult:
    source = Path(action.source)
    target = Path(action.target) if action.target else None
    source_before = sha256(source)
    target_before = sha256(target) if target else None
    if not apply:
        return ActionResult(
            action.kind,
            action.reason,
            action.source,
            action.target,
            source_before,
            target_before,
            target_before,
            "dry_run",
            "planned",
        )
    if source_before is None:
        return ActionResult(
            action.kind,
            action.reason,
            action.source,
            action.target,
            None,
            target_before,
            target_before,
            "already_absent",
            "skipped",
        )
    try:
        if action.kind == "trash":
            method = trasher(source)
            return ActionResult(
                action.kind,
                action.reason,
                action.source,
                None,
                source_before,
                None,
                None,
                method,
                "applied",
            )
        if target is None:
            raise ValueError("replacement target is required")
        if source.resolve(strict=False) == target.resolve(strict=False):
            raise ValueError("replacement source and target are identical")
        if target_before == source_before:
            method = f"target_already_current+{trasher(source)}"
        else:
            target_method = atomic_copy(source, target, trasher)
            target_after_copy = sha256(target)
            if target_after_copy != source_before:
                raise RuntimeError("target hash mismatch after atomic copy")
            method = f"atomic_replace({target_method})+{trasher(source)}"
        target_after = sha256(target)
        if target_after != source_before:
            raise RuntimeError("target hash mismatch after source cleanup")
        return ActionResult(
            action.kind,
            action.reason,
            action.source,
            action.target,
            source_before,
            target_before,
            target_after,
            method,
            "applied",
        )
    except Exception as exc:
        return ActionResult(
            action.kind,
            action.reason,
            action.source,
            action.target,
            source_before,
            target_before,
            sha256(target) if target else None,
            "failed",
            "error",
            f"{type(exc).__name__}: {exc}",
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("REAL_ESTATE_ROOT", "/mnt/c/Users/digit/Dropbox/Real Estate")))
    parser.add_argument("--global-ledger-root", type=Path, default=Path("/mnt/c/Users/digit/Dropbox/Projects/assetrail"))
    parser.add_argument("--stale-report", type=Path)
    parser.add_argument("--report", type=Path, default=Path(__file__).resolve().parents[1] / "reports" / "baselane_canonical_financial_artifact_cleanup.json")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    generated_at = utc_now()
    try:
        plan, reviews = build_plan(args.root, args.stale_report, args.global_ledger_root)
        results = [execute_action(action, args.apply) for action in plan]
        failures = [result for result in results if result.status == "error"]
        status = "error" if failures else ("review" if reviews else "ok")
    except Exception as exc:
        plan = []
        results = []
        reviews = [{"code": "cleanup_scan_failed", "detail": f"{type(exc).__name__}: {exc}"}]
        failures = []
        status = "error"
    payload = {
        "job": "baselane-canonical-financial-artifact-cleanup",
        "generated_at": generated_at,
        "status": status,
        "apply_requested": args.apply,
        "real_estate_root": str(args.root),
        "global_ledger_root": str(args.global_ledger_root),
        "stale_report": str(args.stale_report) if args.stale_report else None,
        "planned_action_count": len(plan),
        "applied_action_count": sum(result.status == "applied" for result in results),
        "failure_count": len(failures),
        "review_count": len(reviews),
        "reviews": reviews,
        "actions": [asdict(result) for result in results],
        "policy": {
            "dry_run_default": True,
            "recoverable_delete_preferred": True,
            "one_current_general_ledger_and_cash_flow_statement_per_property": True,
            "preserve_pre_2026_history": True,
            "preserve_lofty_transactions_exports": True,
            "canonical_financials_markdown_suffix": "/".join(CANONICAL_FINANCIALS_SUFFIX),
        },
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: payload[key] for key in ("status", "planned_action_count", "applied_action_count", "failure_count", "review_count")}))
    return 1 if status == "error" else (2 if status == "review" else 0)


if __name__ == "__main__":
    raise SystemExit(main())
