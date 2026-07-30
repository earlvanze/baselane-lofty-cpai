#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lofty_index_status import is_active_index_status, is_excluded_index_status, normalize_index_status
from lofty_monthly_exclusions import DEFAULT_MANUAL_EXCLUDED_PROPERTIES, match_exclusion_guard, monthly_exclusion_guards
from lofty_property_paths import public_dir_for_property, resolve_index_property_path
from lofty_financial_approval_manifest import approved_candidate, load as load_financial_approval_manifest
from lofty_update_approval_manifest import approved_candidate as approved_update_candidate, load as load_update_approval_manifest

UPDATES_DIR_NAME = "00 - README & Property Snapshot"
FINANCIALS_DIR_NAME = "00 - README & Property Snapshot"
GUARD_TIMEOUT_SECONDS = 30
SAFE_MONTHLY_CRON_DRY_RUN_COMMAND = (
    "DRY_RUN=1 SEND_OWNER_EMAILS=0 PUBLISH_LOFTY_PM_UPDATES=0 "
    "RUN_LOFTY_GUARDED_APPLY=1 APPLY_LOFTY_GUARDED_UPDATES=0 bash scripts/baselane_financials_monthly_cron.sh"
)
LIVE_UPDATE_CAPTURE_REPORT = Path("reports/baselane_financials_monthly_live_update_capture.json")
CURRENT_ONLY_VERIFY_REPORT = Path("reports/lofty_listing_update_cleanup_queue.local-live-verify.json")
POWERSHELL_EXE = shutil.which("powershell.exe") or "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"


def safe_path(path: Path) -> Path:
    expanded = path.expanduser()
    if os.environ.get("LOFTY_SKIP_PROPERTY_SIBLING_RESOLUTION") == "1":
        return expanded if expanded.is_absolute() else Path.cwd() / expanded
    return expanded.resolve()


def safe_live_guard_next_action(target: str) -> str:
    guard_name = "lofty-updates-guard" if target == "UPDATES.md" else "lofty-live-file-guard"
    return (
        f"Auth Lofty visible tab (3 tries), then refresh live {target} guard evidence for {guard_name} through the safe monthly dry-run. "
        f"Run `{SAFE_MONTHLY_CRON_DRY_RUN_COMMAND}`; this keeps email, Lofty PM publish, and guarded live writes disabled."
    )


def classified_guard_next_action(target: str, first_error: str | None) -> str:
    error = (first_error or "").lower()
    if "not reconciled to the latest registered live lofty snapshot" in error:
        return (
            f"Registered live {target} snapshot is fresh; reconcile local {target} to the fetched live text, "
            "then rerun guarded monthly dry-run before any Lofty publish or owner email."
        )
    if "live lofty listing updates field contains updates.md history" in error:
        return (
            "Live Lofty listing update field contains full UPDATES.md history; run the latest-only listing cleanup queue "
            "and publish only the approved current-month snippet per active property after explicit live-update approval, "
            "then rerun guarded monthly dry-run. Owner email remains held."
        )
    if "live-fetch artifact is stale" in error or "no registered live-fetch" in error or "auth" in error:
        return safe_live_guard_next_action(target)
    return (
        f"Inspect the {target} guard error, reconcile live/local state, then rerun guarded monthly dry-run before any "
        "Lofty publish or owner email."
    )


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run_guard(command: list[str]) -> dict[str, Any]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=GUARD_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        return {
            "return_code": 124,
            "stdout": (exc.stdout or "")[-2000:] if isinstance(exc.stdout, str) else "",
            "stderr": f"guard command timed out after {GUARD_TIMEOUT_SECONDS}s",
            "ok": False,
            "timed_out": True,
        }
    return {
        "return_code": result.returncode,
        "stdout": result.stdout[-2000:],
        "stderr": result.stderr[-2000:],
        "ok": result.returncode == 0,
    }


def read_text(path: Path) -> str:
    placeholder_error = cloud_placeholder_error(path)
    if placeholder_error:
        windows_text = read_text_via_windows(path)
        if windows_text is not None:
            return windows_text
        if hydrate_placeholder_via_windows(path):
            windows_text = read_text_via_windows(path)
            if windows_text is not None:
                return windows_text
            return path.read_text(encoding="utf-8")
        raise placeholder_error
    return path.read_text(encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    if not write_text_via_windows(path, text):
        path.write_text(text, encoding="utf-8")


def windows_path(path: Path) -> str | None:
    raw_path = str(path)
    if not raw_path.startswith("/mnt/") or len(raw_path) < 7 or raw_path[6] != "/":
        return None
    drive = raw_path[5].upper()
    return f"{drive}:\\" + raw_path[7:].replace("/", "\\")


def read_text_via_windows(path: Path) -> str | None:
    win_path = windows_path(path)
    if not win_path:
        return None
    command = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        f"$p={powershell_literal(win_path)}; "
        "Get-Content -LiteralPath $p -Raw -ErrorAction Stop"
    )
    try:
        result = subprocess.run(
            [POWERSHELL_EXE, "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def hydrate_placeholder_via_windows(path: Path) -> bool:
    win_path = windows_path(path)
    if not win_path:
        return False
    command = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        f"$p={powershell_literal(win_path)}; "
        "$item=Get-Item -LiteralPath $p -ErrorAction Stop; "
        "$stream=[System.IO.File]::Open($item.FullName,[System.IO.FileMode]::Open,[System.IO.FileAccess]::Read,[System.IO.FileShare]::ReadWrite); "
        "try { $buffer=New-Object byte[] ([Math]::Min(4096, [Math]::Max(1, $stream.Length))); [void]$stream.Read($buffer,0,$buffer.Length) } finally { $stream.Dispose() }"
    )
    try:
        result = subprocess.run(
            [POWERSHELL_EXE, "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def windows_temp_dir() -> Path | None:
    temp_dir = Path("/mnt/c/Users/digit/AppData/Local/Temp")
    return temp_dir if temp_dir.is_dir() else None


def write_text_via_windows(path: Path, text: str) -> bool:
    win_path = windows_path(path)
    temp_dir = windows_temp_dir()
    if not win_path or not temp_dir:
        return False
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            prefix="openclaw-dropbox-write-",
            suffix=".tmp",
            dir=temp_dir,
            delete=False,
        ) as handle:
            handle.write(text)
            temp_path = Path(handle.name)
        win_temp_path = windows_path(temp_path)
        if not win_temp_path:
            return False
        command = (
            "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
            f"$src={powershell_literal(win_temp_path)}; "
            f"$dst={powershell_literal(win_path)}; "
            "$dir=Split-Path -LiteralPath $dst -Parent; "
            "New-Item -ItemType Directory -Force -LiteralPath $dir | Out-Null; "
            "$tmp=$dst + '.tmp'; "
            "Copy-Item -LiteralPath $src -Destination $tmp -Force; "
            "Move-Item -LiteralPath $tmp -Destination $dst -Force"
        )
        result = subprocess.run(
            [POWERSHELL_EXE, "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass




def cloud_placeholder_error(path: Path) -> OSError | None:
    try:
        stat_result = path.stat()
    except OSError:
        return None
    if (
        getattr(stat_result, "st_blocks", None) == 0
        and getattr(stat_result, "st_size", 0) > 0
        and "/Dropbox/" in str(path)
    ):
        return OSError(
            11,
            f"Dropbox online-only placeholder is not hydrated: {path} (size={stat_result.st_size}, blocks=0)",
        )
    return None


def exception_summary(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_json_report(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def full_history_update_guard_verified(updates_md: Path, property_path: Path) -> dict[str, Any] | None:
    capture = load_json_report(LIVE_UPDATE_CAPTURE_REPORT)
    if not capture:
        return None
    if capture.get("status") != "ok":
        return None
    target_count = capture.get("target_count")
    if not target_count or capture.get("check_ok_count") != target_count:
        return None
    if capture.get("mismatch_count") not in (0, None) or capture.get("unverified_count") not in (0, None):
        return None
    if capture.get("external_mutation_count") not in (0, None):
        return None
    if capture.get("sends_owner_email") is not False:
        return None
    status_counts = capture.get("record_status_counts")
    if not isinstance(status_counts, dict) or status_counts.get("guard_ok") != target_count:
        return None
    updates_md_text = str(updates_md)
    property_path_text = str(property_path)
    for item in capture.get("records") or []:
        if not isinstance(item, dict) or item.get("status") != "guard_ok":
            continue
        if item.get("updates_md") != updates_md_text and item.get("property_path") != property_path_text:
            continue
        return {
            "status": "guard_ok",
            "live_update_capture_report": str(LIVE_UPDATE_CAPTURE_REPORT),
            "lofty_property_id": item.get("lofty_property_id"),
            "live_char_count": item.get("live_updates_length"),
            "listing_update_scope": "full_history",
            "local_updates_md_scope": "historical_mirror",
        }
    return None


def apply_local_financials(financials_md: Path, approved_financials: Path, approved_text: str) -> dict[str, Any]:
    before_text = ""
    current_placeholder = cloud_placeholder_error(financials_md)
    try:
        if current_placeholder:
            before_text = ""
        else:
            before_text = read_text(financials_md)
        write_text(financials_md, approved_text.rstrip("\n") + "\n")
        after_text = read_text(financials_md)
    except (OSError, UnicodeError) as exc:
        return {
            "ok": False,
            "target_path": str(financials_md),
            "approved_draft": str(approved_financials),
            "backup_path": None,
            "error": exception_summary(exc),
        }
    return {
        "ok": True,
        "target_path": str(financials_md),
        "approved_draft": str(approved_financials),
        "backup_path": None,
        "backup_created": False,
        "replaced_unreadable_placeholder": bool(current_placeholder),
        "before_sha256": sha256_text(before_text),
        "after_sha256": sha256_text(after_text),
        "approved_sha256": sha256_text(approved_text.rstrip("\n") + "\n"),
    }


def approved_update_candidates(draft_path: Path, run_month: str, approval_dir: Path | None = None) -> list[Path]:
    candidates: list[Path] = []
    if draft_path.name.endswith(("-approved.md", "-reviewed.md")):
        candidates.append(draft_path)
    if draft_path.name.endswith("-draft.md"):
        candidates.append(draft_path.with_name(draft_path.name.removesuffix("-draft.md") + "-approved.md"))
        candidates.append(draft_path.with_name(draft_path.name.removesuffix("-draft.md") + "-reviewed.md"))
    candidates.extend(
        [
            draft_path.parent / f"{run_month}-owner-update-approved.md",
            draft_path.parent / f"{run_month}-owner-update-reviewed.md",
        ]
    )
    if approval_dir is not None and safe_path(approval_dir) != safe_path(draft_path.parent):
        if draft_path.name.endswith("-draft.md"):
            candidates.append(approval_dir / (draft_path.name.removesuffix("-draft.md") + "-approved.md"))
            candidates.append(approval_dir / (draft_path.name.removesuffix("-draft.md") + "-reviewed.md"))
        candidates.extend(
            [
                approval_dir / f"{run_month}-owner-update-approved.md",
                approval_dir / f"{run_month}-owner-update-reviewed.md",
            ]
            )
    seen: set[Path] = set()
    unique: list[Path] = []
    for candidate in candidates:
        resolved = safe_path(candidate)
        if resolved not in seen:
            unique.append(resolved)
            seen.add(resolved)
    return unique


def approved_update_candidates_for_record(draft_path: Path | None, run_month: str, approval_dir: Path) -> list[Path]:
    if draft_path is not None:
        return approved_update_candidates(draft_path, run_month, approval_dir)
    candidates = [
        *approved_update_candidates(approval_dir / f"{run_month}-owner-update-draft.md", run_month, approval_dir),
        *approved_update_candidates(approval_dir / f"{run_month}-owner-update-checkin-draft.md", run_month, approval_dir),
    ]
    seen: set[Path] = set()
    unique: list[Path] = []
    for candidate in candidates:
        resolved = safe_path(candidate)
        if resolved not in seen:
            unique.append(resolved)
            seen.add(resolved)
    return unique


def approved_financials_text_issues(text: str) -> list[str]:
    lower_text = text.lower()
    issues: list[str] = []
    if "review before investor email/publish" in lower_text:
        issues.append("generated_review_required_marker")
    if "no reviewed markdown `financials.md` source existed yet" in lower_text:
        issues.append("generated_without_reviewed_financials_source")
    if "\n## ledger summary" in lower_text or lower_text.startswith("## ledger summary"):
        issues.append("ledger_summary_only")
    return issues


def approved_update_text_issues(text: str) -> list[str]:
    issues: list[str] = []
    if "This month's update is limited to verified cash-position data from Lofty and ECO records." in text:
        issues.append("limited_verified_cash_position_language")
    if "No tenant ledger rows are included." in text:
        issues.append("tenant_ledger_exclusion_language")
    has_financial_detail = any(marker in text for marker in ("Financial detail:", "Financial summary from FINANCIALS.md:"))
    if has_financial_detail and not re.search(r"(?:\bas of\s+|\()\d{4}-\d{2}\b", text, re.I):
        issues.append("missing_financials_md_summary_as_of_month")
    return issues


def approved_update_text_reporting_issues(text: str) -> list[str]:
    issues = approved_update_text_issues(text)
    if not any(marker in text for marker in ("Financial detail:", "Financial summary from FINANCIALS.md:")):
        issues.append("missing_financials_md_summary")
    return issues


def first_existing(paths: list[Path]) -> Path | None:
    for path in paths:
        if path.is_file():
            return path
    return None


def apply_update_record(
    row: dict[str, str],
    run_month: str,
    updates_guard: Path,
    live_guard: Path,
    do_apply: bool,
    financial_approval_manifest: dict[str, Any],
    update_approval_manifest: dict[str, Any],
) -> dict[str, Any]:
    property_path, path_resolution = resolve_index_property_path(row)
    row_status = normalize_index_status(row.get("status"))
    draft_path_value = row.get("draft_path") or ""
    draft_path = safe_path(Path(draft_path_value)) if draft_path_value else None
    public_dir = public_dir_for_property(property_path)
    updates_md = public_dir / UPDATES_DIR_NAME / "UPDATES.md"
    financials_md = public_dir / FINANCIALS_DIR_NAME / "FINANCIALS.md"
    record: dict[str, Any] = {
        "property_path": str(property_path),
        "index_status": row_status,
        "draft_path": str(draft_path) if draft_path else None,
        "updates_md": str(updates_md),
        "financials_md": str(financials_md),
        "updates": {},
        "financials": {},
        **path_resolution,
    }

    if is_excluded_index_status(row.get("status")):
        record["updates"] = {"status": row_status, "notes": row.get("notes") or ""}
        record["financials"] = {"status": row_status, "notes": row.get("notes") or ""}
        return record

    if not updates_md.is_file():
        record["updates"] = {"status": "missing_updates_md"}
    else:
        update_approval = approved_update_candidate(
            update_approval_manifest,
            run_month=run_month,
            canonical_updates=updates_md,
        )
        if not update_approval:
            record["updates"] = {
                "status": "no_manifest_approved_update_candidate",
                "approval_manifest_required": True,
            }
        else:
            approved_entry = Path(str(update_approval["candidate_path"]))
            try:
                approved_text = read_text(approved_entry).strip()
            except (OSError, UnicodeError) as exc:
                record["updates"] = {
                    "status": "approved_entry_read_error",
                    "approved_entry": str(approved_entry),
                    "error": exception_summary(exc),
                }
                approved_text = None
            if approved_text is not None:
                try:
                    current_text = read_text(updates_md)
                except (OSError, UnicodeError) as exc:
                    record["updates"] = {
                        "status": "updates_md_read_error",
                        "approved_entry": str(approved_entry),
                        "error": exception_summary(exc),
                    }
                    current_text = None
            else:
                current_text = None
            if current_text is None:
                pass
            elif approved_text and approved_text in current_text:
                approved_issues = approved_update_text_issues(approved_text)
                reporting_issues = approved_update_text_reporting_issues(approved_text)
                if approved_issues:
                    record["updates"] = {
                        "status": "approved_entry_unreviewed",
                        "approved_entry": str(approved_entry),
                        "approved_update_quality_issues": reporting_issues,
                    }
                else:
                    record["updates"] = {"status": "already_applied", "approved_entry": str(approved_entry)}
                    if reporting_issues:
                        record["updates"]["approved_update_quality_issues"] = reporting_issues
            elif not approved_text:
                record["updates"] = {"status": "approved_entry_blank", "approved_entry": str(approved_entry)}
            else:
                approved_issues = approved_update_text_issues(approved_text)
                reporting_issues = approved_update_text_reporting_issues(approved_text)
                if approved_issues:
                    record["updates"] = {
                        "status": "approved_entry_unreviewed",
                        "approved_entry": str(approved_entry),
                        "approved_update_quality_issues": reporting_issues,
                    }
                else:
                    check = run_guard([sys.executable, str(updates_guard), "check", str(updates_md)])
                    record["updates"]["check"] = check
                    record["updates"]["approved_entry"] = str(approved_entry)
                    if reporting_issues:
                        record["updates"]["approved_update_quality_issues"] = reporting_issues
                    if not check["ok"]:
                        full_history_proof = full_history_update_guard_verified(updates_md, property_path)
                        if full_history_proof:
                            record["updates"]["status"] = "ready"
                            record["updates"]["check"]["accepted_full_history_listing_proof"] = full_history_proof
                        else:
                            record["updates"]["status"] = "guard_failed"
                    elif not do_apply:
                        record["updates"]["status"] = "ready"
                    else:
                        apply_result = run_guard([sys.executable, str(updates_guard), "prepend", str(updates_md), str(approved_entry)])
                        record["updates"]["apply"] = apply_result
                        record["updates"]["status"] = "applied" if apply_result["ok"] else "apply_failed"

    if not financials_md.is_file():
        record["financials"] = {"status": "missing_financials_md"}
    else:
        approval = approved_candidate(
            financial_approval_manifest,
            run_month=run_month,
            canonical_financials=financials_md,
        )
        if not approval:
            record["financials"] = {
                "status": "no_manifest_approved_financial_candidate",
                "approval_manifest_required": True,
            }
        else:
            approved_financials = Path(str(approval["candidate_path"]))
            record["financials"]["approved_candidate"] = str(approved_financials)
            record["financials"]["approved_candidate_sha256"] = approval["candidate_sha256"]
            try:
                approved_text = read_text(approved_financials).strip()
            except (OSError, UnicodeError) as exc:
                record["financials"]["status"] = "approved_financials_read_error"
                record["financials"]["error"] = exception_summary(exc)
                approved_text = None
            if approved_text is not None:
                try:
                    current_text = read_text(financials_md).strip()
                except (OSError, UnicodeError) as exc:
                    record["financials"]["status"] = "financials_md_read_error"
                    record["financials"]["error"] = exception_summary(exc)
                    current_text = None
            else:
                current_text = None
            if current_text is not None:
                approved_issues = approved_financials_text_issues(approved_text)
                if not approved_text:
                    record["financials"]["status"] = "approved_financials_blank"
                elif approved_issues:
                    record["financials"]["status"] = "approved_financials_unreviewed"
                    record["financials"]["approved_financials_quality_issues"] = approved_issues
                elif approved_text == current_text:
                    record["financials"]["status"] = "already_applied"
                elif not do_apply:
                    record["financials"]["status"] = "ready"
                else:
                    apply_result = apply_local_financials(financials_md, approved_financials, approved_text)
                    record["financials"]["apply"] = apply_result
                    record["financials"]["status"] = "applied" if apply_result["ok"] else "apply_failed"
            elif approved_text is not None and cloud_placeholder_error(financials_md):
                approved_issues = approved_financials_text_issues(approved_text)
                if not approved_text:
                    record["financials"]["status"] = "approved_financials_blank"
                elif approved_issues:
                    record["financials"]["status"] = "approved_financials_unreviewed"
                    record["financials"]["approved_financials_quality_issues"] = approved_issues
                elif not do_apply:
                    record["financials"]["status"] = "ready"
                    record["financials"]["current_read_warning"] = "canonical FINANCIALS.md is an unreadable Dropbox placeholder and will be replaced by the approved snapshot on apply"
                else:
                    apply_result = apply_local_financials(financials_md, approved_financials, approved_text)
                    record["financials"]["apply"] = apply_result
                    record["financials"]["status"] = "applied" if apply_result["ok"] else "apply_failed"
    return record


def excluded_update_record(row: dict[str, str], exclusion: dict[str, Any]) -> dict[str, Any]:
    property_path, path_resolution = resolve_index_property_path(row)
    row_status = normalize_index_status(row.get("status"))
    public_dir = public_dir_for_property(property_path)
    status = "excluded_no_live_update_or_email"
    return {
        "property_path": str(property_path),
        "index_status": row_status,
        "updates_md": str(public_dir / UPDATES_DIR_NAME / "UPDATES.md"),
        "financials_md": str(public_dir / FINANCIALS_DIR_NAME / "FINANCIALS.md"),
        "exclude_source": exclusion.get("source"),
        "exclude_reason": exclusion.get("exclude_reason"),
        "matched_exclusion_property": exclusion.get("property_name"),
        "yhome_column_b": exclusion.get("yhome_column_b"),
        "updates": {"status": status},
        "financials": {"status": status},
        **path_resolution,
    }


def pending_update_record(row: dict[str, str]) -> dict[str, Any]:
    property_path, path_resolution = resolve_index_property_path(row)
    row_status = normalize_index_status(row.get("status"))
    draft_path_value = row.get("draft_path") or ""
    draft_path = safe_path(Path(draft_path_value)) if draft_path_value else None
    public_dir = public_dir_for_property(property_path)
    return {
        "property_path": str(property_path),
        "index_status": row_status,
        "draft_path": str(draft_path) if draft_path else None,
        "updates_md": str(public_dir / UPDATES_DIR_NAME / "UPDATES.md"),
        "financials_md": str(public_dir / FINANCIALS_DIR_NAME / "FINANCIALS.md"),
        "updates": {"status": "pending_guard_check"},
        "financials": {"status": "pending_guard_check"},
        **path_resolution,
    }


def financial_hold_exclusions(path: Path | None) -> list[dict[str, Any]]:
    report = load_json_report(path) if path and path.is_file() else None
    details = (report or {}).get("property_cash_review_details")
    if not isinstance(details, list):
        return []
    exclusions: list[dict[str, Any]] = []
    for detail in details:
        if not isinstance(detail, dict) or str(detail.get("source_clean_status") or "").strip().lower() == "ok":
            continue
        property_name = str(detail.get("property") or detail.get("property_name") or "").strip()
        if property_name:
            exclusions.append(
                {
                    "source": "transfer_reconciliation_financial_hold",
                    "property_name": property_name,
                    "normalized_property": re.sub(r"[^a-z0-9]+", " ", property_name.lower()).strip(),
                    "exclude_reason": "property financial truth is held pending source-cash review",
                }
            )
    return exclusions


def summarize(records: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        for section in ("updates", "financials"):
            status = record.get(section, {}).get("status") or "unknown"
            counts[f"{section}.{status}"] = counts.get(f"{section}.{status}", 0) + 1
    return counts


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp")
    temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp_path.replace(path)


def build_report(
    *,
    run_month: str,
    apply: bool,
    records: list[dict[str, Any]],
    issues: list[str],
    yhome_guard: dict[str, Any],
    manual_exclusions: list[dict[str, Any]],
    in_progress: bool = False,
) -> dict[str, Any]:
    counts = summarize(records)
    actionable_blockers = build_blocker_summary(counts, records)
    status_issues = [f"{item['class']}={item['count']}" for item in actionable_blockers]
    report_issues = [*issues, *status_issues]
    blocking_statuses = {
        "updates.guard_failed",
        "updates.apply_failed",
        "updates.approved_entry_read_error",
        "updates.updates_md_read_error",
        "updates.approved_entry_blank",
        "updates.approved_entry_unreviewed",
        "financials.guard_failed",
        "financials.apply_failed",
        "financials.approved_financials_read_error",
        "financials.financials_md_read_error",
        "financials.approved_financials_blank",
        "financials.approved_financials_unreviewed",
    }
    review_statuses = {
        "updates.missing_updates_md",
        "updates.missing_monthly_draft",
        "updates.needs_reviewed_entry",
        "updates.no_manifest_approved_update_candidate",
        "updates.pending_guard_check",
        "financials.missing_financials_md",
        "financials.no_manifest_approved_financial_candidate",
        "financials.pending_guard_check",
    }
    if in_progress:
        status = "in_progress"
    elif any(counts.get(status, 0) for status in blocking_statuses):
        status = "failed"
    elif any(counts.get(status, 0) for status in review_statuses):
        status = "review"
    else:
        status = "ok"
    external_excluded_records = [
        record
        for record in records
        if record.get("updates", {}).get("status") == "excluded_no_live_update_or_email"
        and record.get("financials", {}).get("status") == "excluded_no_live_update_or_email"
    ]
    skipped_closed_statuses = {"skipped_closed", "closed", "skipped_sold", "sold", "skipped_delisted", "delisted"}
    skipped_closed_records = [
        record
        for record in records
        if str(record.get("updates", {}).get("status") or "") in skipped_closed_statuses
        and str(record.get("financials", {}).get("status") or "") in skipped_closed_statuses
    ]
    excluded_total_records = [*external_excluded_records, *skipped_closed_records]
    return {
        "generated_at": iso_z(),
        "run_month": run_month,
        "apply": apply,
        "status": "failed" if issues and not in_progress else status,
        "in_progress": in_progress,
        "issues": report_issues,
        "input_issues": issues,
        "blocker_count": len(actionable_blockers),
        "actionable_blockers": actionable_blockers,
        "counts": counts,
        "record_count": len(records),
        "excluded_property_count": len(excluded_total_records),
        "excluded_property_names": [
            Path(str(record.get("property_path") or "")).name
            for record in excluded_total_records
        ],
        "externally_excluded_property_count": len(external_excluded_records),
        "externally_excluded_property_names": [
            Path(str(record.get("property_path") or "")).name
            for record in external_excluded_records
        ],
        "skipped_closed_property_count": len(skipped_closed_records),
        "skipped_closed_property_names": [
            Path(str(record.get("property_path") or "")).name
            for record in skipped_closed_records
        ],
        "excluded_total_property_count": len(excluded_total_records),
        "excluded_total_property_names": [
            Path(str(record.get("property_path") or "")).name
            for record in excluded_total_records
        ],
        "yhome_transition_guard": yhome_guard,
        "manual_excluded_property_names": [record["property_name"] for record in manual_exclusions],
        "records": records,
    }


def first_record_for_status(records: list[dict[str, Any]], section: str, status: str) -> dict[str, Any] | None:
    for record in records:
        if record.get(section, {}).get("status") == status:
            return record
    return None


def compact_guard_stderr(record: dict[str, Any] | None, section: str) -> str | None:
    if not record:
        return None
    section_record = record.get(section, {})
    error = str(section_record.get("error") or "").strip()
    if error:
        first_line = next((line.strip() for line in error.splitlines() if line.strip()), "")
        return first_line[:300] or None
    check = section_record.get("check")
    if not isinstance(check, dict):
        return None
    stderr = str(check.get("stderr") or "").strip()
    if not stderr:
        return None
    first_line = next((line.strip() for line in stderr.splitlines() if line.strip()), "")
    return first_line[:300] or None


def blocker_samples(records: list[dict[str, Any]], section: str, status: str, limit: int = 5) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for record in records:
        section_record = record.get(section, {})
        if section_record.get("status") != status:
            continue
        samples.append(
            {
                "property": Path(str(record.get("property_path") or "")).name,
                "property_path": record.get("property_path"),
                "target": record.get(f"{section}_md"),
                "approved_artifact": section_record.get("approved_entry") or section_record.get("approved_draft"),
                "error": compact_guard_stderr(record, section),
            }
        )
        if len(samples) >= limit:
            break
    return samples


def build_blocker_summary(counts: dict[str, int], records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    specs = {
        "updates.guard_failed": (
            safe_live_guard_next_action("UPDATES.md"),
            "updates",
            "guard_failed",
        ),
        "financials.guard_failed": (
            safe_live_guard_next_action("FINANCIALS.md"),
            "financials",
            "guard_failed",
        ),
        "updates.apply_failed": ("Inspect failed lofty-updates-guard prepend result before retrying.", "updates", "apply_failed"),
        "financials.apply_failed": ("Inspect failed lofty-live-file-guard apply result before retrying.", "financials", "apply_failed"),
        "updates.approved_entry_read_error": (
            "Hydrate/read approved owner-update Dropbox placeholder files locally, then rerun guarded monthly dry-run; "
            "on Windows, select the listed approved artifacts in Explorer and choose 'Make available offline'.",
            "updates",
            "approved_entry_read_error",
        ),
        "updates.updates_md_read_error": ("Fix filesystem readability for canonical UPDATES.md, then rerun guarded monthly dry-run.", "updates", "updates_md_read_error"),
        "updates.approved_entry_blank": ("Replace blank approved owner-update entry before applying.", "updates", "approved_entry_blank"),
        "updates.approved_entry_unreviewed": (
            "Replace stale/unreviewed approved owner-update entry with a reviewed update that includes the FINANCIALS.md summary and no limited cash-position language.",
            "updates",
            "approved_entry_unreviewed",
        ),
        "financials.approved_financials_read_error": (
            "Fix filesystem readability for approved FINANCIALS.md draft, then rerun guarded monthly dry-run.",
            "financials",
            "approved_financials_read_error",
        ),
        "financials.financials_md_read_error": (
            "Fix filesystem readability for canonical FINANCIALS.md, then rerun guarded monthly dry-run.",
            "financials",
            "financials_md_read_error",
        ),
        "financials.approved_financials_blank": (
            "Replace blank approved FINANCIALS.md draft before applying.",
            "financials",
            "approved_financials_blank",
        ),
        "financials.approved_financials_unreviewed": (
            "Replace generated/unreviewed approved FINANCIALS.md snapshot with a reviewed monthly financial snapshot before applying.",
            "financials",
            "approved_financials_unreviewed",
        ),
        "updates.missing_updates_md": ("Restore canonical Public/00 - README & Property Snapshot/UPDATES.md.", "updates", "missing_updates_md"),
        "updates.missing_monthly_draft": ("Generate or locate the monthly owner-update draft.", "updates", "missing_monthly_draft"),
        "updates.needs_reviewed_entry": ("Create reviewed/approved owner-update entry before applying.", "updates", "needs_reviewed_entry"),
        "updates.no_manifest_approved_update_candidate": (
            "Record an explicit hash-bound approval for the current monthly owner-update candidate before applying.",
            "updates",
            "no_manifest_approved_update_candidate",
        ),
        "updates.pending_guard_check": ("Wait for guarded monthly apply review to finish checking every active property.", "updates", "pending_guard_check"),
        "financials.missing_financials_md": ("Restore canonical Public/00 - README & Property Snapshot/FINANCIALS.md.", "financials", "missing_financials_md"),
        "financials.no_manifest_approved_financial_candidate": (
            "Record an explicit hash-bound approval for the current monthly FINANCIALS candidate before applying.",
            "financials",
            "no_manifest_approved_financial_candidate",
        ),
        "financials.pending_guard_check": ("Wait for guarded monthly apply review to finish checking every active property.", "financials", "pending_guard_check"),
    }
    blockers: list[dict[str, Any]] = []
    for key in sorted(counts):
        count = counts.get(key, 0)
        if count <= 0 or key not in specs:
            continue
        next_action, section, status = specs[key]
        first = first_record_for_status(records, section, status)
        first_error = compact_guard_stderr(first, section)
        if key == "updates.guard_failed":
            next_action = classified_guard_next_action("UPDATES.md", first_error)
        elif key == "financials.guard_failed":
            next_action = classified_guard_next_action("FINANCIALS.md", first_error)
        blockers.append(
            {
                "class": key,
                "count": count,
                "first_property": Path(str(first.get("property_path") or "")).name if first else None,
                "first_target": first.get(f"{section}_md") if first else None,
                "first_error": first_error,
                "samples": blocker_samples(records, section, status),
                "next_action": next_action,
            }
        )
    return blockers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply reviewed monthly Lofty update artifacts only after guard checks pass.")
    parser.add_argument("--index-csv", required=True, type=Path)
    parser.add_argument("--run-month", required=True)
    parser.add_argument("--updates-guard", required=True, type=Path)
    parser.add_argument("--live-guard", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--yhome-transition-csv", type=Path)
    parser.add_argument("--manual-excluded-property", action="append", default=[])
    parser.add_argument("--transfer-reconciliation-report", type=Path)
    parser.add_argument("--financial-approval-manifest", type=Path, default=Path("reports/lofty_financial_approval_manifest.json"))
    parser.add_argument("--update-approval-manifest", type=Path, default=Path("reports/lofty_update_approval_manifest.json"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    issues: list[str] = []
    records: list[dict[str, Any]] = []
    if not args.index_csv.is_file():
        issues.append(f"monthly index missing: {args.index_csv}")
    if not args.updates_guard.is_file():
        issues.append(f"updates guard missing: {args.updates_guard}")
    if not args.live_guard.is_file():
        issues.append(f"live-file guard missing: {args.live_guard}")
    financial_approval_manifest = load_financial_approval_manifest(args.financial_approval_manifest)
    update_approval_manifest = load_update_approval_manifest(args.update_approval_manifest)

    if not issues:
        manual_names = [*DEFAULT_MANUAL_EXCLUDED_PROPERTIES, *args.manual_excluded_property]
        exclusion_guards, yhome_guard, manual_exclusions = monthly_exclusion_guards(args.yhome_transition_csv, manual_names)
        exclusion_guards.extend(financial_hold_exclusions(args.transfer_reconciliation_report))
        rows: list[dict[str, str]] = []
        resolved_exclusions: dict[int, dict[str, Any]] = {}
        with args.index_csv.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                status = row.get("status") or ""
                if not is_active_index_status(status) and not is_excluded_index_status(status):
                    continue
                row_index = len(rows)
                rows.append(row)
                if is_active_index_status(status):
                    property_path, _path_resolution = resolve_index_property_path(row)
                    exclusion = match_exclusion_guard(property_path, exclusion_guards)
                    if exclusion:
                        resolved_exclusions[row_index] = exclusion
        if not rows:
            issues.append(f"monthly index has no property rows: {args.index_csv}")

        if not issues:
            records = [
                excluded_update_record(row, resolved_exclusions[index])
                if index in resolved_exclusions
                else pending_update_record(row)
                for index, row in enumerate(rows)
            ]
            write_json_atomic(
                args.report,
                build_report(
                    run_month=args.run_month,
                    apply=args.apply,
                    records=records,
                    issues=issues,
                    yhome_guard=yhome_guard,
                    manual_exclusions=manual_exclusions,
                    in_progress=True,
                ),
            )

            for index, row in enumerate(rows):
                if index in resolved_exclusions:
                    continue
                records[index] = apply_update_record(
                    row,
                    args.run_month,
                    args.updates_guard,
                    args.live_guard,
                    args.apply,
                    financial_approval_manifest,
                    update_approval_manifest,
                )
                write_json_atomic(
                    args.report,
                    build_report(
                        run_month=args.run_month,
                        apply=args.apply,
                        records=records,
                        issues=issues,
                        yhome_guard=yhome_guard,
                        manual_exclusions=manual_exclusions,
                        in_progress=True,
                    ),
                )
    else:
        yhome_guard = {"status": "not_checked", "path": str(args.yhome_transition_csv) if args.yhome_transition_csv else None, "excluded_count": 0}
        manual_exclusions = []

    report = build_report(
        run_month=args.run_month,
        apply=args.apply,
        records=records,
        issues=issues,
        yhome_guard=yhome_guard,
        manual_exclusions=manual_exclusions,
    )
    write_json_atomic(args.report, report)
    print(json.dumps({k: report[k] for k in ("status", "counts", "record_count", "issues")}, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
