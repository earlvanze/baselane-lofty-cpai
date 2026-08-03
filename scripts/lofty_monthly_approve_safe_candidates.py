#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lofty_financial_approval_manifest import sha256_file, write as write_financial_approval_manifest
from lofty_update_approval_manifest import write as write_update_approval_manifest

POWERSHELL_EXE = shutil.which("powershell.exe") or "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def comms_workspace_candidates() -> list[Path]:
    candidates: list[Path] = []
    env_path = os.environ.get("COMMS_WORKSPACE")
    if env_path:
        candidates.append(Path(env_path))
    root = Path(__file__).resolve().parents[1]
    candidates.extend(
        [
            root.parent / "workspace-lofty-vp",
            Path("/home/digit/.openclaw/workspace-lofty-vp"),
            root.parent / "workspace-lofty-vp-comms",
            Path("/home/digit/.openclaw/workspace-lofty-vp-comms"),
        ]
    )
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            unique.append(candidate)
            seen.add(key)
    return unique


def resolve_rent_roll_source_path(path: Path | None, run_month: str | None) -> Path | None:
    if path is None:
        return None
    if path.is_file():
        return path
    if not run_month:
        return path
    for comms_workspace in comms_workspace_candidates():
        candidate = comms_workspace / "updates" / f"{run_month}-rent-roll-source.json"
        if candidate.is_file():
            return candidate
    return path


def windows_path(path: Path) -> str | None:
    raw_path = str(path)
    if not raw_path.startswith("/mnt/") or len(raw_path) < 7 or raw_path[6] != "/":
        return None
    drive = raw_path[5].upper()
    return f"{drive}:\\" + raw_path[7:].replace("/", "\\")


def likely_unavailable_placeholder(path: Path) -> bool:
    try:
        stat_result = path.stat()
    except OSError:
        return False
    return stat_result.st_size > 0 and getattr(stat_result, "st_blocks", 1) == 0 and "/Dropbox/" in str(path)


def read_text(path: Path) -> str:
    if likely_unavailable_placeholder(path):
        windows_text = read_text_via_windows(path)
        if windows_text is not None:
            return windows_text
    return path.read_text(encoding="utf-8")


def candidate_text_issues(text: str, section: str) -> list[str]:
    issues: list[str] = []
    if section == "update":
        if "This month's update is limited to verified cash-position data from Lofty and ECO records." in text:
            issues.append("limited_verified_cash_position_language")
        if "No tenant ledger rows are included." in text:
            issues.append("tenant_ledger_exclusion_language")
        if not any(marker in text for marker in ("Financial detail:", "Financial summary from FINANCIALS.md:")):
            issues.append("missing_financials_md_summary")
        if not re.search(r"(?:\bas of\s+|\()\d{4}-\d{2}\b", text, re.I):
            issues.append("missing_as_of_month")
    if section == "financial":
        misleading_definitions = (
            "ECO Net DAO Funds is the full DAO-attributed Column E sum",
            "ECO Net DAO Funds is the full property General Ledger net position",
            "ECO Operating Cash is the current complete DAO-attributed total of Column E",
        )
        if any(marker in text for marker in misleading_definitions):
            issues.append("misleading_eco_net_dao_funds_definition")
        if "ECO Net DAO Funds (spendable cash held by ECO)" not in text:
            issues.append("missing_spendable_eco_cash_definition")
        if re.search(
            r"ECO Net DAO Funds \(spendable cash held by ECO\)(?:\s*\||:)\s*Pending reconciliation",
            text,
            re.I,
        ):
            issues.append("eco_net_dao_funds_reconciliation_pending")
        pending_balance_labels = (
            "Accessible DAO funds for operations",
            "Spendable Baselane/ECO cash after recorded obligations",
            "Recorded unpaid obligations",
            "Category detail",
            "DAO A/P - Due to ECO (unreimbursed cash advances)",
            "ECO A/R - Due from DAO (same intercompany balance)",
        )
        for label in pending_balance_labels:
            if re.search(rf"^\s*-\s*{re.escape(label)}:\s*Pending reconciliation\s*$", text, re.I | re.M):
                issues.append(f"{label.lower().replace(' ', '_').replace('/', '_')}_reconciliation_pending")
        if re.search(r"^\s*-\s+.*:\s*Pending reconciliation\s*$", text, re.I | re.M):
            issues.append("balance_sheet_reconciliation_pending")
        if re.search(r"Cash held by the mortgage servicer for taxes and insurance\s*\|\s*Pending reconciliation", text, re.I):
            issues.append("mortgage_escrow_reconciliation_pending")
    return issues


def write_text(path: Path, text: str) -> None:
    try:
        path.write_text(text, encoding="utf-8")
        return
    except OSError:
        if not write_text_via_windows(path, text):
            raise


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


def copy_candidate(source: Path, target: Path, apply: bool) -> dict[str, Any]:
    record = {
        "source": str(source),
        "target": str(target),
        "source_exists": source.is_file(),
        "target_exists_before": target.is_file() if str(target) not in {"", "."} else False,
        "replaces_existing": False,
        "superseded_target": None,
        "applied": False,
        "status": "not_started",
    }
    if str(target) in {"", "."}:
        record["status"] = "missing_target"
        return record
    if not source.is_file():
        record["status"] = "missing_source"
        return record
    text = read_text(source).strip()
    if not text:
        record["status"] = "blank_source"
        return record
    target_is_placeholder = target.is_file() and likely_unavailable_placeholder(target)
    if target_is_placeholder:
        record["target_unreadable_placeholder"] = True
        if not apply:
            record["status"] = "would_approve"
            return record
        write_text(target, text + "\n")
        record["applied"] = True
        record["hydrated_placeholder"] = True
        record["status"] = "approved"
        return record
    if target.is_file():
        if read_text(target).strip() == text:
            record["status"] = "already_approved"
            return record
        # A prior month-stamped approval is evidence, not a mutable staging file.
        # Replacing it would let an automatic run silently approve revised finance.
        record["replaces_existing"] = True
        record["status"] = "existing_approval_target_mismatch_requires_review"
        return record
    if not apply:
        record["status"] = "would_approve"
        return record
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    write_text(tmp, text + "\n")
    if not write_text_via_windows(target, text + "\n"):
        tmp.replace(target)
    elif tmp.exists():
        tmp.unlink()
    record["applied"] = True
    record["status"] = "approved"
    return record


def unique_superseded_path(target: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = target.with_name(f"{target.stem}.superseded-{stamp}{target.suffix}")
    if not base.exists():
        return base
    for index in range(1, 1000):
        candidate = target.with_name(f"{target.stem}.superseded-{stamp}.{index}{target.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not allocate superseded path for {target}")


def blocked_candidate(source: Path, target: Path, reason: str) -> dict[str, Any]:
    return {
        "source": str(source),
        "target": str(target),
        "source_exists": source.is_file(),
        "target_exists_before": target.is_file() if str(target) not in {"", "."} else False,
        "applied": False,
        "status": "blocked",
        "reason": reason,
    }


def safety_ok(safety_scan: dict[str, Any]) -> tuple[bool, list[str]]:
    issues: list[str] = []
    if not safety_scan:
        issues.append("missing_safety_scan")
    if safety_scan.get("status") != "ok":
        issues.append(f"safety_status_{safety_scan.get('status')}")
    for key in ("high_count", "medium_count", "missing_count"):
        if int(safety_scan.get(key) or 0) != 0:
            issues.append(f"{key}_{safety_scan.get(key)}")
    return not issues, issues


def normalized_key(value: object) -> str:
    return str(value or "").strip().lower()


def normalized_property_key(value: object) -> str:
    text = re.sub(r"\bpublic\b", " ", str(value or ""), flags=re.I)
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def listing_update_exclusion_keys(policy: dict[str, Any] | None) -> set[str]:
    keys: set[str] = set()
    for field in ("sold_ignore_listing_updates", "operational_ignore_listing_updates"):
        values = (policy or {}).get(field) or []
        if not isinstance(values, list):
            continue
        for value in values:
            raw_value = value if isinstance(value, dict) else {}
            name = raw_value.get("address") or raw_value.get("property_name") or (value if not isinstance(value, dict) else "")
            key = normalized_property_key(name)
            if key:
                keys.add(key)
    return keys


def packet_record_property_keys(packet_record: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for field in ("property_name", "input_property_name", "property_path", "input_property_path"):
        value = packet_record.get(field)
        if not value:
            continue
        key = normalized_property_key(value)
        if key:
            keys.add(key)
        try:
            path_name = Path(str(value)).name
        except OSError:
            path_name = ""
        path_key = normalized_property_key(path_name)
        if path_key:
            keys.add(path_key)
    return keys


def excluded_by_listing_update_policy(packet_record: dict[str, Any], exclusion_keys: set[str]) -> bool:
    if not exclusion_keys:
        return False
    property_keys = packet_record_property_keys(packet_record)
    for property_key in property_keys:
        for exclusion_key in exclusion_keys:
            if property_key == exclusion_key or property_key in exclusion_key or exclusion_key in property_key:
                return True
    return False


def financial_hold_names(transfer_reconciliation: dict[str, Any] | None) -> set[str]:
    details = (transfer_reconciliation or {}).get("property_cash_review_details")
    if not isinstance(details, list):
        return set()
    return {
        normalized_key(detail.get("property") or detail.get("property_name"))
        for detail in details
        if isinstance(detail, dict)
        and str(detail.get("source_clean_status") or "").strip().lower() != "ok"
        and normalized_key(detail.get("property") or detail.get("property_name"))
    }


def property_keys_match(left: str, right: str) -> bool:
    """Match report property labels to public-folder labels without broad fuzzy matches."""
    variants = {
        normalized_key(left),
        normalized_key(left).removesuffix("public").strip(),
        normalized_key(right),
        normalized_key(right).removesuffix("public").strip(),
    }
    variants.discard("")
    return len(variants) < 4 and bool(variants)


def count(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def section_from_issue(issue: object) -> set[str]:
    text = str(issue or "").strip().lower()
    if text.startswith("update candidate failed"):
        return {"update"}
    if text.startswith("financial candidate failed"):
        return {"financial"}
    if text.startswith("monthly financial summary"):
        return {"update", "financial"}
    return {"update", "financial"}


def candidate_section_issues(packet_record: dict[str, Any], section: str) -> list[str]:
    section_issues: list[str] = []
    for issue in packet_record.get("issues") or []:
        if section in section_from_issue(issue):
            section_issues.append(str(issue))
    for marker in packet_record.get("markers") or []:
        marker_text = str(marker or "")
        if marker_text.startswith(f"{section}."):
            section_issues.append(f"marker:{marker_text}")
    return section_issues


def safety_findings_by_record(safety_scan: dict[str, Any]) -> dict[str, dict[str, list[str]]]:
    findings_by_record: dict[str, dict[str, list[str]]] = {}
    for scan_record in safety_scan.get("records") or []:
        if not isinstance(scan_record, dict):
            continue
        keys = {
            normalized_key(scan_record.get("property_path")),
            normalized_key(scan_record.get("property_name")),
        }
        keys.discard("")
        section_findings: dict[str, list[str]] = {"update": [], "financial": []}
        for finding in scan_record.get("findings") or []:
            if not isinstance(finding, dict):
                continue
            raw_section = str(finding.get("section") or "")
            section = "update" if raw_section == "updates" else "financial" if raw_section == "financials" else raw_section
            if section not in section_findings:
                section_findings.setdefault(section, [])
            section_findings[section].append(
                f"{finding.get('severity')}:{finding.get('class')}:{finding.get('message')}"
            )
        for key in keys:
            findings_by_record[key] = section_findings
    return findings_by_record


def safety_unmatched_issues(safety_scan: dict[str, Any]) -> list[str]:
    if not safety_scan:
        return ["missing_safety_scan"]
    findings = [finding for record in safety_scan.get("records") or [] for finding in (record.get("findings") or [])]
    severity_counts = {
        "high_count": sum(1 for finding in findings if finding.get("severity") == "high"),
        "medium_count": sum(1 for finding in findings if finding.get("severity") == "medium"),
        "missing_count": sum(1 for finding in findings if finding.get("class") == "missing_scan_source"),
    }
    issues: list[str] = []
    for key, observed in severity_counts.items():
        reported = int(safety_scan.get(key) or 0)
        if reported > observed:
            issues.append(f"unmatched_{key}_{reported}_observed_{observed}")
    if safety_scan.get("status") != "ok" and not findings and any(int(safety_scan.get(key) or 0) for key in ("high_count", "medium_count", "missing_count")):
        issues.append(f"safety_status_{safety_scan.get('status')}_without_findings")
    return issues


def record_safety_findings(
    packet_record: dict[str, Any],
    section: str,
    findings_by_record: dict[str, dict[str, list[str]]],
) -> list[str]:
    keys = [
        normalized_key(packet_record.get("property_path")),
        normalized_key(packet_record.get("input_property_path")),
        normalized_key(packet_record.get("property_name")),
        normalized_key(packet_record.get("input_property_name")),
    ]
    for key in keys:
        if key and key in findings_by_record:
            return findings_by_record[key].get(section) or []
    return []


def blocked_or_copy_candidate(
    source: Path,
    target: Path,
    apply: bool,
    blockers: list[str],
    section: str,
) -> dict[str, Any]:
    if source.is_file():
        try:
            blockers.extend(candidate_text_issues(read_text(source), section))
        except OSError as exc:
            blockers.append(f"candidate_read_error:{exc}")
    if blockers:
        return blocked_candidate(source, target, ";".join(blockers))
    return copy_candidate(source, target, apply)


def approve_financial_candidate(
    source: Path,
    canonical_financials: Path,
    apply: bool,
    blockers: list[str],
) -> dict[str, Any]:
    """Approve the exact candidate digest without creating a duplicate Markdown file."""
    if source.is_file():
        try:
            blockers.extend(candidate_text_issues(read_text(source), "financial"))
        except OSError as exc:
            blockers.append(f"candidate_read_error:{exc}")
    if blockers:
        return blocked_candidate(source, canonical_financials, ";".join(blockers))
    if not source.is_file():
        return blocked_candidate(source, canonical_financials, "missing_source")
    record = {
        "source": str(source),
        "target": str(canonical_financials),
        "source_exists": True,
        "target_exists_before": canonical_financials.is_file(),
        "candidate_sha256": sha256_file(source),
        "applied": apply,
        "status": "approved" if apply else "would_approve",
        "approval_storage": "digest_manifest",
    }
    return record


def approve_update_candidate(
    source: Path,
    canonical_updates: Path,
    apply: bool,
    blockers: list[str],
) -> dict[str, Any]:
    """Approve the exact update digest without creating a staging Markdown copy."""
    if source.is_file():
        try:
            blockers.extend(candidate_text_issues(read_text(source), "update"))
        except OSError as exc:
            blockers.append(f"candidate_read_error:{exc}")
    if blockers:
        return blocked_candidate(source, canonical_updates, ";".join(blockers))
    if not source.is_file():
        return blocked_candidate(source, canonical_updates, "missing_source")
    return {
        "source": str(source),
        "target": str(canonical_updates),
        "source_exists": True,
        "target_exists_before": canonical_updates.is_file(),
        "candidate_sha256": sha256_file(source),
        "applied": apply,
        "status": "approved" if apply else "would_approve",
        "approval_storage": "digest_manifest",
    }


def rent_roll_source_ok(rent_roll_source: dict[str, Any] | None) -> tuple[bool, list[str]]:
    issues: list[str] = []
    if rent_roll_source is None:
        return True, issues
    if not rent_roll_source:
        issues.append("missing_rent_roll_source")
        return False, issues
    if rent_roll_source.get("status") != "ok":
        issues.append(f"rent_roll_source_status_{rent_roll_source.get('status')}")
    if rent_roll_source.get("freshness_status") != "current":
        issues.append(f"rent_roll_source_freshness_{rent_roll_source.get('freshness_status')}")
    has_current_export = int(rent_roll_source.get("current_month_export_count") or 0) > 0
    has_current_source = (
        rent_roll_source.get("source_current") is True
        and int(rent_roll_source.get("source_file_count") or 0) > 0
        and not rent_roll_source.get("source_blocker_count")
    )
    if not (has_current_export or has_current_source):
        issues.append("rent_roll_source_current_month_export_missing")
    if rent_roll_source.get("owner_email_allowed") is False:
        issues.append("rent_roll_source_owner_email_blocked")
    if rent_roll_source.get("live_update_allowed") is False:
        issues.append("rent_roll_source_live_update_blocked")
    return not issues, issues


def update_blocked_by_rent_roll_only(record: dict[str, Any]) -> bool:
    update = record.get("update") if isinstance(record.get("update"), dict) else {}
    return update.get("status") == "blocked" and update.get("reason") == "rent_roll_source_not_current"


def rent_roll_hold_next_action(rent_roll_source: dict[str, Any] | None) -> str:
    latest = (rent_roll_source or {}).get("latest_exported_on")
    latest_part = f" latest_exported_on={latest};" if latest else ""
    return (
        f"Refresh Hemlane rent-roll source to current month;{latest_part} then rerun safe monthly dry-run before "
        "approving owner update snippets, Lofty listing publish, or owner email."
    )


def build_report(
    candidate_packet: dict[str, Any],
    safety_scan: dict[str, Any],
    apply: bool,
    rent_roll_source: dict[str, Any] | None = None,
    transfer_reconciliation: dict[str, Any] | None = None,
    listing_update_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    issues: list[str] = []
    section_safety_findings = safety_findings_by_record(safety_scan)
    hard_safety_issues = safety_unmatched_issues(safety_scan)
    issues.extend(hard_safety_issues)
    _, rent_roll_issues = rent_roll_source_ok(rent_roll_source)
    if not candidate_packet:
        issues.append("missing_candidate_packet")
    elif candidate_packet.get("status") != "ok":
        issues.append(f"candidate_packet_status_{candidate_packet.get('status')}")
        if int(candidate_packet.get("issue_count") or 0) != 0:
            issues.append(f"candidate_packet_issue_count_{candidate_packet.get('issue_count')}")
        if int(candidate_packet.get("marker_count") or 0) != 0:
            issues.append(f"candidate_packet_marker_count_{candidate_packet.get('marker_count')}")

    records: list[dict[str, Any]] = []
    held_names = financial_hold_names(transfer_reconciliation)
    held_properties: list[str] = []
    exclusion_keys = listing_update_exclusion_keys(listing_update_policy)
    excluded_properties: list[str] = []
    if candidate_packet and not hard_safety_issues:
        for packet_record in candidate_packet.get("records") or []:
            property_name = str(packet_record.get("property_name") or "").strip()
            property_key = normalized_key(property_name)
            if packet_record.get("live_publish_excluded") is True:
                excluded_properties.append(property_name)
                continue
            if excluded_by_listing_update_policy(packet_record, exclusion_keys):
                excluded_properties.append(property_name)
                continue
            if any(property_keys_match(held, property_key) for held in held_names):
                held_properties.append(property_name)
                continue
            update_source = Path(str(packet_record.get("update_candidate") or ""))
            canonical_updates = Path(str(packet_record.get("updates_md") or ""))
            financial_source = Path(str(packet_record.get("financial_candidate") or ""))
            canonical_financials = Path(str(packet_record.get("financials_md") or ""))
            update_blockers = (
                candidate_section_issues(packet_record, "update")
                + record_safety_findings(packet_record, "update", section_safety_findings)
            )
            if rent_roll_issues:
                update_blockers.append("rent_roll_source_not_current")
            financial_blockers = candidate_section_issues(packet_record, "financial") + record_safety_findings(
                packet_record,
                "financial",
                section_safety_findings,
            )
            record = {
                "property_name": packet_record.get("property_name"),
                "property_path": packet_record.get("property_path"),
                "update": approve_update_candidate(update_source, canonical_updates, apply, update_blockers),
                "financial": approve_financial_candidate(financial_source, canonical_financials, apply, financial_blockers),
            }
            records.append(record)
    issues.extend(rent_roll_issues)
    if held_properties:
        issues.extend(f"financial_hold_property:{property_name}" for property_name in held_properties)

    status_counts: dict[str, int] = {}
    for record in records:
        for section in ("update", "financial"):
            status = str((record.get(section) or {}).get("status") or "unknown")
            status_counts[f"{section}.{status}"] = status_counts.get(f"{section}.{status}", 0) + 1

    blocking = [
        status
        for status in status_counts
        if (
            status.endswith(".missing_source")
            or status.endswith(".blank_source")
            or status.endswith(".missing_target")
            or status.endswith(".blocked")
            or status.endswith(".existing_approval_target_mismatch_requires_review")
        )
    ]
    rent_roll_blocked_update_count = sum(1 for record in records if update_blocked_by_rent_roll_only(record))
    non_rent_roll_blocking = [
        status
        for status in blocking
        if status != "update.blocked" or rent_roll_blocked_update_count != status_counts.get("update.blocked", 0)
    ]
    rent_roll_hold_only = (
        bool(rent_roll_issues)
        and bool(records)
        and rent_roll_blocked_update_count == len(records)
        and not hard_safety_issues
        and not non_rent_roll_blocking
        and count(candidate_packet.get("issue_count")) == 0
        and count(candidate_packet.get("marker_count")) == 0
    )
    if issues or blocking:
        status = "review"
    elif apply:
        status = "ok"
    else:
        status = "ok_dry_run"

    return {
        "generated_at": iso_z(),
        "status": status,
        "apply": apply,
        "issues": issues,
        "issue_count": len(issues),
        "rent_roll_source_configured": rent_roll_source is not None,
        "rent_roll_source_status": (rent_roll_source or {}).get("status") if rent_roll_source is not None else None,
        "rent_roll_source_freshness_status": (rent_roll_source or {}).get("freshness_status") if rent_roll_source is not None else None,
        "rent_roll_source_current_month_export_count": (rent_roll_source or {}).get("current_month_export_count") if rent_roll_source is not None else None,
        "rent_roll_source_latest_exported_on": (rent_roll_source or {}).get("latest_exported_on") if rent_roll_source is not None else None,
        "rent_roll_source_owner_email_allowed": (rent_roll_source or {}).get("owner_email_allowed") if rent_roll_source is not None else None,
        "rent_roll_source_live_update_allowed": (rent_roll_source or {}).get("live_update_allowed") if rent_roll_source is not None else None,
        "property_count": len(records),
        "excluded_property_count": len(excluded_properties),
        "excluded_properties": excluded_properties,
        "financial_hold_property_count": len(held_properties),
        "financial_hold_properties": held_properties,
        "status_counts": status_counts,
        "approved_update_count": sum(1 for record in records if record["update"]["status"] in {"approved", "already_approved", "would_approve"}),
        "approved_financial_count": sum(1 for record in records if record["financial"]["status"] in {"approved", "already_approved", "would_approve"}),
        "rent_roll_hold_only": rent_roll_hold_only,
        "rent_roll_blocked_update_count": rent_roll_blocked_update_count,
        "next_action": rent_roll_hold_next_action(rent_roll_source) if rent_roll_hold_only else "",
        "records": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Promote safety-scanned monthly review candidates to approved artifacts.")
    parser.add_argument("--candidate-packet", required=True, type=Path)
    parser.add_argument("--safety-scan", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--rent-roll-source", type=Path)
    parser.add_argument("--transfer-reconciliation-report", type=Path)
    parser.add_argument("--listing-update-policy", type=Path)
    parser.add_argument("--run-month")
    parser.add_argument("--financial-approval-manifest", type=Path, default=Path("reports/lofty_financial_approval_manifest.json"))
    parser.add_argument("--update-approval-manifest", type=Path, default=Path("reports/lofty_update_approval_manifest.json"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    candidate_packet = load_json(args.candidate_packet)
    run_month = args.run_month or candidate_packet.get("run_month")
    rent_roll_source_path = resolve_rent_roll_source_path(args.rent_roll_source, str(run_month or ""))
    rent_roll_source = load_json(rent_roll_source_path) if rent_roll_source_path else None
    transfer_reconciliation = load_json(args.transfer_reconciliation_report) if args.transfer_reconciliation_report else None
    listing_update_policy = load_json(args.listing_update_policy) if args.listing_update_policy else None
    report = build_report(
        candidate_packet,
        load_json(args.safety_scan),
        args.apply,
        rent_roll_source=rent_roll_source,
        transfer_reconciliation=transfer_reconciliation,
        listing_update_policy=listing_update_policy,
    )
    if args.apply:
        approved_updates = [
            {
                "property_name": record.get("property_name"),
                "canonical_updates": str((record.get("update") or {}).get("target") or ""),
                "candidate_path": str((record.get("update") or {}).get("source") or ""),
                "candidate_sha256": str((record.get("update") or {}).get("candidate_sha256") or ""),
                "approved": True,
            }
            for record in report.get("records") or []
            if (record.get("update") or {}).get("status") == "approved"
        ]
        approved_financials = [
            {
                "property_name": record.get("property_name"),
                "canonical_financials": str((record.get("financial") or {}).get("target") or ""),
                "candidate_path": str((record.get("financial") or {}).get("source") or ""),
                "candidate_sha256": str((record.get("financial") or {}).get("candidate_sha256") or ""),
                "approved": True,
            }
            for record in report.get("records") or []
            if (record.get("financial") or {}).get("status") == "approved"
        ]
        write_financial_approval_manifest(
            args.financial_approval_manifest,
            run_month=str(run_month or ""),
            approvals=approved_financials,
        )
        report["financial_approval_manifest"] = str(args.financial_approval_manifest)
        report["financial_approval_manifest_count"] = len(approved_financials)
        write_update_approval_manifest(
            args.update_approval_manifest,
            run_month=str(run_month or ""),
            approvals=approved_updates,
        )
        report["update_approval_manifest"] = str(args.update_approval_manifest)
        report["update_approval_manifest_count"] = len(approved_updates)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "apply", "property_count", "approved_update_count", "approved_financial_count", "issue_count")}, indent=2, sort_keys=True))
    return 0 if report["status"] in {"ok", "ok_dry_run"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
