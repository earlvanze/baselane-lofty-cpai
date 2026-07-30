#!/usr/bin/env python3
"""Restore archived co-owner-paid mortgage statements into canonical folders.

This copies statement files out of the duplicate-parent archive produced by the
public-folder reorg. It is intentionally conservative: only files classified as
monthly statement evidence by the tokenomics statement rules are copied, and
existing destination files are never overwritten.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, TextIO

SCRIPT_PATH = Path(__file__).absolute()
WORKSPACE_ROOT = SCRIPT_PATH.parents[1]
SCRIPTS_DIR = WORKSPACE_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import update_coownership_mortgage_tokenomics as tokenomics
from stable_json_report import stable_report_digest, write_json_report

DEFAULT_REPORT = WORKSPACE_ROOT / "reports" / "coownership_mortgage_archive_restore_report.json"


def default_real_estate_root() -> Path:
    if os.environ.get("REAL_ESTATE_ROOT"):
        return Path(os.environ["REAL_ESTATE_ROOT"])
    if os.environ.get("DROPBOX_ROOT"):
        return Path(os.environ["DROPBOX_ROOT"]) / "Real Estate"
    return tokenomics.REAL_ESTATE_ROOT


def first_destination_root(real_estate_root: Path, property_name: str) -> Path | None:
    rels = tokenomics.PROPERTY_DIR_REL_CANDIDATES.get(property_name) or []
    return real_estate_root / rels[0] if rels else None


def statement_date_for_restore(path: Path) -> str | None:
    parsed = tokenomics.parse_date_from_name(path.name)
    if not parsed and path.suffix.lower() == ".pdf":
        parsed = tokenomics.extract_statement_date(path)
    return parsed.isoformat() if parsed else None


def is_monthly_statement(path: Path) -> tuple[bool, str | None, str | None]:
    if path.suffix.lower() not in tokenomics.STATEMENT_EXTENSIONS:
        return False, None, "unsupported_extension"
    score = tokenomics.statement_name_score(path)
    statement_date = statement_date_for_restore(path)
    if score <= 0:
        return False, statement_date, "not_monthly_statement"
    if not statement_date:
        return False, None, "missing_statement_date"
    return True, statement_date, None


def previous_month(value: str | None) -> str | None:
    match = re.match(r"^(20\d{2})-(\d{2})$", str(value or ""))
    if not match:
        return None
    year = int(match.group(1))
    month = int(match.group(2))
    if month == 1:
        return f"{year - 1}-12"
    return f"{year}-{month - 1:02d}"


def current_month_freshness(path: Path, statement_date: str | None, target_month: str | None) -> dict[str, Any]:
    if not target_month or not statement_date:
        return {
            "target_month": target_month,
            "current_month_statement_available": target_month is None,
            "statement_month_status": None,
            "statement_month": statement_date[:7] if statement_date else None,
            "payment_due_date": None,
            "payment_due_month": None,
            "current_month_basis": None,
        }
    try:
        parsed_date = date.fromisoformat(statement_date)
    except ValueError:
        parsed_date = None
    statement_month = statement_date[:7]
    if parsed_date and statement_month not in {target_month, previous_month(target_month)}:
        freshness = tokenomics.statement_month_status(True, statement_date, target_month)
        return {
            "target_month": target_month,
            "current_month_statement_available": freshness.get("current_month_statement_available") is True,
            "statement_month_status": freshness.get("statement_month_status"),
            "statement_month": freshness.get("statement_month"),
            "payment_due_date": freshness.get("payment_due_date"),
            "payment_due_month": freshness.get("payment_due_month"),
            "current_month_basis": freshness.get("current_month_basis"),
        }
    freshness = tokenomics.statement_freshness_for_path(path, parsed_date, target_month)
    return {
        "target_month": target_month,
        "current_month_statement_available": freshness.get("current_month_statement_available") is True,
        "statement_month_status": freshness.get("statement_month_status"),
        "statement_month": freshness.get("statement_month"),
        "payment_due_date": freshness.get("payment_due_date"),
        "payment_due_month": freshness.get("payment_due_month"),
        "current_month_basis": freshness.get("current_month_basis"),
    }


def iter_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(path for path in root.rglob("*") if path.is_file())


def latest_existing_statement_evidence(
    real_estate_root: Path,
    property_name: str,
    destination_root: Path | None,
    *,
    target_month: str | None = None,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "latest_existing_statement_found": False,
        "latest_existing_statement_count": 0,
        "latest_existing_statement_path": None,
        "latest_existing_statement_relative": None,
        "latest_existing_statement_filename": None,
        "latest_existing_statement_date": None,
        "latest_existing_statement_month": None,
        "latest_existing_statement_month_status": None,
        "latest_existing_current_month_statement_available": False,
        "latest_existing_payment_due_date": None,
        "latest_existing_payment_due_month": None,
        "latest_existing_current_month_basis": None,
    }
    if destination_root is None or not destination_root.is_dir():
        return evidence

    candidates: list[dict[str, Any]] = []
    for path in iter_files(destination_root):
        is_statement, statement_date, _skip_reason = is_monthly_statement(path)
        if not is_statement or not statement_date:
            continue
        freshness = current_month_freshness(path, statement_date, target_month)
        try:
            relative = str(path.relative_to(real_estate_root))
        except ValueError:
            relative = str(path)
        candidates.append(
            {
                "property": property_name,
                "path": str(path),
                "relative_path": relative,
                "filename": path.name,
                "statement_date": statement_date,
                "statement_month": freshness.get("statement_month") or statement_date[:7],
                "statement_month_status": freshness.get("statement_month_status"),
                "current_month_statement_available": freshness.get("current_month_statement_available") is True,
                "payment_due_date": freshness.get("payment_due_date"),
                "payment_due_month": freshness.get("payment_due_month"),
                "current_month_basis": freshness.get("current_month_basis"),
            }
        )

    evidence["latest_existing_statement_count"] = len(candidates)
    if not candidates:
        return evidence

    latest = sorted(
        candidates,
        key=lambda item: (
            str(item.get("statement_date") or ""),
            str(item.get("path") or ""),
        ),
    )[-1]
    evidence.update(
        {
            "latest_existing_statement_found": True,
            "latest_existing_statement_path": latest.get("path"),
            "latest_existing_statement_relative": latest.get("relative_path"),
            "latest_existing_statement_filename": latest.get("filename"),
            "latest_existing_statement_date": latest.get("statement_date"),
            "latest_existing_statement_month": latest.get("statement_month"),
            "latest_existing_statement_month_status": latest.get("statement_month_status"),
            "latest_existing_current_month_statement_available": latest.get("current_month_statement_available") is True,
            "latest_existing_payment_due_date": latest.get("payment_due_date"),
            "latest_existing_payment_due_month": latest.get("payment_due_month"),
            "latest_existing_current_month_basis": latest.get("current_month_basis"),
        }
    )
    return evidence


def build_copy_plan(
    real_estate_root: Path,
    property_name: str,
    *,
    target_month: str | None = None,
    current_month_only: bool = False,
) -> dict[str, Any]:
    destination_root = first_destination_root(real_estate_root, property_name)
    archive_roots = tokenomics.archive_dir_candidates(real_estate_root).get(property_name, [])
    entries: list[dict[str, Any]] = []
    copied_candidates = 0
    skipped_candidates = 0
    conflict_count = 0
    planned_destinations: set[Path] = set()

    for archive_root in archive_roots:
        archive_exists = archive_root.is_dir()
        if not archive_exists:
            continue
        for source in iter_files(archive_root):
            is_statement, statement_date, skip_reason = is_monthly_statement(source)
            if not is_statement:
                skipped_candidates += 1
                entries.append(
                    {
                        "property": property_name,
                        "source": str(source),
                        "archive_root": str(archive_root),
                        "destination": None,
                        "statement_date": statement_date,
                        "action": "skip",
                        "reason": skip_reason,
                    }
                )
                continue
            freshness = current_month_freshness(source, statement_date, target_month)
            if current_month_only and freshness.get("current_month_statement_available") is not True:
                skipped_candidates += 1
                entries.append(
                    {
                        "property": property_name,
                        "source": str(source),
                        "archive_root": str(archive_root),
                        "destination": None,
                        "statement_date": statement_date,
                        "statement_month": freshness.get("statement_month") or statement_date[:7],
                        "action": "skip",
                        "reason": "not_current_month_statement",
                        **freshness,
                    }
                )
                continue
            if destination_root is None:
                skipped_candidates += 1
                entries.append(
                    {
                        "property": property_name,
                        "source": str(source),
                        "archive_root": str(archive_root),
                        "destination": None,
                        "statement_date": statement_date,
                        "action": "skip",
                        "reason": "destination_root_unconfigured",
                    }
                )
                continue
            rel = source.relative_to(archive_root)
            if destination_root.name and rel.parts and rel.parts[0] == destination_root.name:
                rel = Path(*rel.parts[1:])
            destination = destination_root / rel
            action = "copy"
            reason = "ready_to_restore"
            if destination in planned_destinations:
                action = "skip"
                reason = "duplicate_archive_source_destination"
                skipped_candidates += 1
            elif destination.exists():
                try:
                    destination_size = destination.stat().st_size
                    source_size = source.stat().st_size
                except OSError:
                    destination_size = None
                    source_size = None
                same_size = destination_size == source_size and destination_size is not None
                if destination_size == 0 and source_size and source_size > 0:
                    action = "replace_empty"
                    reason = "empty_destination_partial"
                    copied_candidates += 1
                else:
                    action = "skip"
                    reason = "already_exists" if same_size else "destination_conflict"
                    conflict_count += int(not same_size)
                    skipped_candidates += 1
            else:
                copied_candidates += 1
            if action in {"copy", "replace_empty"}:
                planned_destinations.add(destination)
            entries.append(
                {
                    "property": property_name,
                    "source": str(source),
                    "archive_root": str(archive_root),
                    "destination": str(destination),
                    "destination_relative": str(destination.relative_to(real_estate_root)),
                    "statement_date": statement_date,
                    "statement_month": statement_date[:7],
                    "action": action,
                    "reason": reason,
                    **freshness,
                }
            )

    return {
        "property": property_name,
        "destination_root": str(destination_root) if destination_root else None,
        "archive_root_count": len([root for root in archive_roots if root.is_dir()]),
        "copy_candidate_count": copied_candidates,
        "skip_count": skipped_candidates,
        "conflict_count": conflict_count,
        "entries": entries,
    }
    plan.update(
        latest_existing_statement_evidence(
            real_estate_root,
            property_name,
            destination_root,
            target_month=target_month,
        )
    )
    return plan


def copy_statement_file(source: Path, destination: Path, *, replace_empty: bool = False) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if replace_empty and destination.exists() and destination.stat().st_size == 0:
        destination.unlink()
    # Do not use shutil.copy2 on /mnt/c; preserving Windows metadata via
    # the WSL 9p mount can block indefinitely. The statement content and
    # destination filename are the durable evidence we need here.
    with source.open("rb") as src, destination.open("xb") as dst:
        while chunk := src.read(1024 * 1024):
            dst.write(chunk)


def apply_copy_plan(plan: dict[str, Any]) -> dict[str, Any]:
    applied = 0
    apply_errors: list[dict[str, str]] = []
    for entry in plan.get("entries") or []:
        if entry.get("action") not in {"copy", "replace_empty"}:
            continue
        source = Path(str(entry.get("source") or ""))
        destination = Path(str(entry.get("destination") or ""))
        original_action = entry.get("action")
        try:
            copy_statement_file(source, destination, replace_empty=original_action == "replace_empty")
            entry["action"] = "copied"
            entry["reason"] = (
                "replaced_empty_destination_from_archive"
                if original_action == "replace_empty"
                else "restored_from_archive"
            )
            applied += 1
        except Exception as exc:  # pragma: no cover - defensive filesystem detail
            entry["action"] = "error"
            entry["reason"] = str(exc)
            apply_errors.append(
                {
                    "source": str(source),
                    "destination": str(destination),
                    "error": str(exc),
                }
            )
    plan["applied_copy_count"] = applied
    plan["apply_error_count"] = len(apply_errors)
    plan["apply_errors"] = apply_errors
    return plan


def plan_status(plan: dict[str, Any], *, apply: bool) -> dict[str, Any]:
    apply_error_count = int(plan.get("apply_error_count") or 0)
    conflict_count = int(plan.get("conflict_count") or 0)
    copy_candidate_count = int(plan.get("copy_candidate_count") or 0)
    applied_copy_count = int(plan.get("applied_copy_count") or 0)
    if apply_error_count:
        return {
            "status": "error",
            "reason": "apply_error",
            "safe_to_run_automatically": False,
            "idempotent_replay_safe": False,
        }
    if conflict_count:
        return {
            "status": "review",
            "reason": "destination_conflict",
            "safe_to_run_automatically": False,
            "idempotent_replay_safe": False,
        }
    if apply and applied_copy_count:
        status = "restored"
        reason = "restored_from_archive"
    elif copy_candidate_count:
        status = "ready_to_restore"
        reason = "copy_candidates_available"
    else:
        status = "idempotent_noop"
        reason = "no_copy_candidates"
    return {
        "status": status,
        "reason": reason,
        "safe_to_run_automatically": True,
        "idempotent_replay_safe": True,
    }


def build_report(
    real_estate_root: Path,
    *,
    apply: bool = False,
    target_month: str | None = None,
    current_month_only: bool = False,
) -> dict[str, Any]:
    properties = sorted(tokenomics.CO_OWNER_PAID_MORTGAGE_PROPERTIES)
    plans = [
        build_copy_plan(
            real_estate_root,
            prop,
            target_month=target_month,
            current_month_only=current_month_only,
        )
        for prop in properties
    ]
    if apply:
        plans = [apply_copy_plan(plan) for plan in plans]
    plans = [{**plan, **plan_status(plan, apply=apply)} for plan in plans]
    copy_candidate_count = sum(int(plan.get("copy_candidate_count") or 0) for plan in plans)
    copied_count = sum(
        1
        for plan in plans
        for entry in plan.get("entries") or []
        if entry.get("action") == "copied"
    )
    conflict_count = sum(int(plan.get("conflict_count") or 0) for plan in plans)
    apply_error_count = sum(int(plan.get("apply_error_count") or 0) for plan in plans)
    latest_existing_statement_records = [
        {
            "property": plan.get("property"),
            "statement_date": plan.get("latest_existing_statement_date"),
            "statement_month": plan.get("latest_existing_statement_month"),
            "statement_month_status": plan.get("latest_existing_statement_month_status"),
            "current_month_statement_available": plan.get("latest_existing_current_month_statement_available") is True,
            "payment_due_date": plan.get("latest_existing_payment_due_date"),
            "payment_due_month": plan.get("latest_existing_payment_due_month"),
            "current_month_basis": plan.get("latest_existing_current_month_basis"),
            "filename": plan.get("latest_existing_statement_filename"),
            "path": plan.get("latest_existing_statement_path"),
            "relative_path": plan.get("latest_existing_statement_relative"),
            "statement_count": plan.get("latest_existing_statement_count"),
        }
        for plan in plans
        if plan.get("latest_existing_statement_found") is True
    ]
    latest_existing_stale_statement_properties = [
        str(item.get("property"))
        for item in latest_existing_statement_records
        if item.get("statement_month_status") == "stale"
    ]
    latest_existing_current_month_statement_properties = [
        str(item.get("property"))
        for item in latest_existing_statement_records
        if item.get("current_month_statement_available") is True
    ]
    status = "error" if apply_error_count else "review" if conflict_count else "ok"
    reason = "apply_error" if apply_error_count else "destination_conflict" if conflict_count else None
    idempotent_replay_safe = status == "ok"
    report = {
        "job": "coownership-mortgage-archive-restore",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "reason": reason,
        "real_estate_root": str(real_estate_root),
        "apply": apply,
        "target_month": target_month,
        "current_month_only": current_month_only,
        "property_count": len(properties),
        "properties": properties,
        "copy_candidate_count": copy_candidate_count,
        "copied_count": copied_count,
        "remaining_copy_candidate_count": 0 if apply and not apply_error_count else copy_candidate_count,
        "conflict_count": conflict_count,
        "apply_error_count": apply_error_count,
        "safe_to_run_automatically": status == "ok",
        "idempotent_replay_safe": idempotent_replay_safe,
        "copy_plan_safe_to_apply_automatically": status == "ok",
        "latest_existing_statement_count": len(latest_existing_statement_records),
        "latest_existing_statement_properties": [
            str(item.get("property")) for item in latest_existing_statement_records
        ],
        "latest_existing_statement_records": latest_existing_statement_records,
        "latest_existing_stale_statement_count": len(latest_existing_stale_statement_properties),
        "latest_existing_stale_statement_properties": latest_existing_stale_statement_properties,
        "latest_existing_current_month_statement_count": len(latest_existing_current_month_statement_properties),
        "latest_existing_current_month_statement_properties": latest_existing_current_month_statement_properties,
        "property_plans": plans,
        "idempotency_digest": None,
    }
    report["idempotency_digest"] = stable_report_digest(report)
    return report


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# Co-Ownership Mortgage Archive Restore",
        "",
        f"- status: `{report.get('status')}`",
        f"- reason: `{report.get('reason')}`",
        f"- apply: `{str(report.get('apply')).lower()}`",
        f"- target_month: `{report.get('target_month')}`",
        f"- current_month_only: `{str(report.get('current_month_only')).lower()}`",
        f"- copy_candidate_count: `{report.get('copy_candidate_count')}`",
        f"- copied_count: `{report.get('copied_count')}`",
        f"- conflict_count: `{report.get('conflict_count')}`",
        f"- apply_error_count: `{report.get('apply_error_count')}`",
        f"- latest_existing_statement_count: `{report.get('latest_existing_statement_count')}`",
        f"- latest_existing_stale_statement_properties: `{report.get('latest_existing_stale_statement_properties')}`",
        f"- latest_existing_current_month_statement_properties: `{report.get('latest_existing_current_month_statement_properties')}`",
        f"- safe_to_run_automatically: `{report.get('safe_to_run_automatically')}`",
        f"- idempotent_replay_safe: `{report.get('idempotent_replay_safe')}`",
        "",
    ]
    for plan in report.get("property_plans") or []:
        if not plan.get("entries") and not plan.get("latest_existing_statement_found"):
            continue
        lines.extend(
            [
                f"## {plan.get('property')}",
                "",
                f"- status: `{plan.get('status')}`",
                f"- reason: `{plan.get('reason')}`",
                f"- safe_to_run_automatically: `{plan.get('safe_to_run_automatically')}`",
                f"- idempotent_replay_safe: `{plan.get('idempotent_replay_safe')}`",
                f"- destination_root: `{plan.get('destination_root')}`",
                f"- copy_candidate_count: `{plan.get('copy_candidate_count')}`",
                f"- skip_count: `{plan.get('skip_count')}`",
                f"- conflict_count: `{plan.get('conflict_count')}`",
                f"- latest_existing_statement_found: `{plan.get('latest_existing_statement_found')}`",
                f"- latest_existing_statement_date: `{plan.get('latest_existing_statement_date')}`",
                f"- latest_existing_statement_month_status: `{plan.get('latest_existing_statement_month_status')}`",
                f"- latest_existing_current_month_statement_available: `{plan.get('latest_existing_current_month_statement_available')}`",
                f"- latest_existing_statement_path: `{plan.get('latest_existing_statement_path')}`",
                "",
            ]
        )
        for entry in plan.get("entries") or []:
            if entry.get("action") not in {"copy", "copied", "error"}:
                continue
            lines.append(
                f"- {entry.get('action')}: `{entry.get('destination')}` "
                f"from `{entry.get('source')}` reason=`{entry.get('reason')}`"
            )
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(lines).rstrip() + "\n"
    if not path.exists() or path.read_text(encoding="utf-8") != content:
        path.write_text(content, encoding="utf-8")


def main(argv: list[str] | None = None, stdout: TextIO = sys.stdout) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-estate-root", type=Path, default=default_real_estate_root())
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_REPORT.with_suffix(".md"))
    parser.add_argument("--apply", action="store_true", help="Copy missing monthly statements into canonical folders")
    parser.add_argument("--target-month", help="YYYY-MM target month used with --current-month-only")
    parser.add_argument(
        "--current-month-only",
        action="store_true",
        help="Only copy statements that satisfy --target-month by statement date or payment due date",
    )
    parser.add_argument("--json", action="store_true", help="Print the JSON report")
    args = parser.parse_args(argv)

    target_month = args.target_month
    if args.current_month_only and not target_month:
        target_month = tokenomics.current_month_name()
    report = build_report(
        args.real_estate_root,
        apply=args.apply,
        target_month=target_month,
        current_month_only=args.current_month_only,
    )
    stable_report = write_json_report(args.report, report)
    write_markdown(args.markdown, stable_report)
    if args.json:
        json.dump(stable_report, stdout, indent=2, sort_keys=True)
        stdout.write("\n")
    return 0 if stable_report.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
