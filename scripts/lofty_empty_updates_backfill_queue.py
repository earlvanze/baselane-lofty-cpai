#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROPERTY_GAP_REASONS = {"updates_md_empty", "updates_md_missing"}
PROPERTY_UPDATE_MARKER_RE = re.compile(r"(?mi)^\s*-\s+\*{0,2}\s*Property Update\s*\(")
DATED_UPDATE_HEADING_RE = re.compile(r"(?mi)^\s*##\s+\d{4}-\d{2}-\d{2}\s*$")
PROPERTY_UPDATES_HEADER_RE = re.compile(r"(?mi)^\s*#\s+Property Updates\s*$")
LOFTY_RESERVE_SUMMARY_RE = re.compile(r"(?mi)^\s*-\s*Lofty-held current maintenance reserve:\s*-?\$[\d,]+\.\d{2}\s*$")
ECO_GL_SUMMARY_RE = re.compile(r"(?mi)^\s*-\s*ECO GL Column E sum:\s*-?\$[\d,]+\.\d{2}(?:\s+\(\d+\s+rows\))?\s*$")
MAX_UPDATE_CHARS = 3500
MAX_UPDATE_LINES = 80
MONTH_RE = re.compile(r"/(\d{4}-\d{2})-[^/]+\.md$")
FORBIDDEN_PATH_MARKERS = (
    "/mnt/f/.openclaw",
    "/home/umbrel",
    "workspace-discord-public",
)


def has_canonical_snapshot_path(path_text: str) -> bool:
    lowered = normalized_path_text(path_text).lower()
    return (
        "/public/00 - readme & property snapshot/" in lowered
        or re.search(r"/[^/]+ public/00 - readme & property snapshot/", lowered) is not None
    )


def has_canonical_financial_path(path_text: str) -> bool:
    lowered = normalized_path_text(path_text).lower()
    return (
        "/public/07 - p&l & owner statements/" in lowered
        or re.search(r"/[^/]+ public/07 - p&l & owner statements/", lowered) is not None
    )


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def stable_digest(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def markdown_escape(value: object) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


def shell_command(parts: list[object]) -> str:
    return " ".join(shlex.quote(str(part)) for part in parts if str(part or "").strip())


def normalized_path_text(value: object) -> str:
    return str(value or "").replace("\\", "/")


def path_hygiene_issues(path_text: str, *, role: str) -> list[str]:
    normalized = normalized_path_text(path_text)
    lowered = normalized.lower()
    issues: list[str] = []
    if not normalized:
        issues.append(f"{role}_path_missing")
        return issues
    for marker in FORBIDDEN_PATH_MARKERS:
        if marker.lower() in lowered:
            issues.append(f"{role}_path_forbidden:{marker}")
    if "/financials/" in lowered:
        issues.append(f"{role}_path_legacy_financials_folder")
    if role in {"updates_md", "approval_target"}:
        if "/dropbox/real estate/" not in lowered:
            issues.append(f"{role}_path_not_dropbox_real_estate")
        if not has_canonical_snapshot_path(normalized):
            issues.append(f"{role}_path_not_canonical_snapshot_folder")
    if role == "updates_md" and not lowered.endswith("/updates.md"):
        issues.append("updates_md_path_not_updates_md")
    if role == "approval_target" and not re.search(r"/\d{4}-\d{2}-owner-update-approved\.md$", lowered):
        issues.append("approval_target_path_not_monthly_approved_update")
    if role == "financial_target":
        if "/dropbox/real estate/" not in lowered:
            issues.append("financial_target_path_not_dropbox_real_estate")
        if not has_canonical_financial_path(normalized):
            issues.append("financial_target_path_not_canonical_pnl_folder")
        if not re.search(r"/\d{4}-\d{2}-financials-approved\.md$", lowered):
            issues.append("financial_target_path_not_monthly_approved_financials")
    if role == "candidate_source":
        if "/reports/lofty-review-candidates/" not in lowered:
            issues.append("candidate_source_path_not_review_candidates")
        if not lowered.endswith(".md"):
            issues.append("candidate_source_path_not_markdown")
    return issues


def command_hygiene_issues(record: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    issues.extend(path_hygiene_issues(str(record.get("updates_md") or ""), role="updates_md"))
    for field, role in (
        ("candidate_update_source", "candidate_source"),
        ("candidate_update_approval_target", "approval_target"),
        ("candidate_financial_approval_target", "financial_target"),
    ):
        value = str(record.get(field) or "")
        if value:
            issues.extend(path_hygiene_issues(value, role=role))
    for field in (
        "local_backfill_command_requires_explicit_approval",
        "approval_copy_command_requires_current_rent_roll_and_explicit_approval",
    ):
        command = str(record.get(field) or "")
        if not command:
            continue
        lowered = command.lower()
        if "cp --" not in lowered:
            issues.append(f"{field}_missing_cp")
        if any(token in lowered for token in (" --send", "publish_latest_update_to_lofty", "mail ", "sendmail")):
            issues.append(f"{field}_contains_external_send_or_publish")
        for marker in FORBIDDEN_PATH_MARKERS:
            if marker.lower() in lowered:
                issues.append(f"{field}_forbidden:{marker}")
    return sorted(set(issues))


def read_gap_rows(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    if not path.is_file():
        return [], [f"property gap CSV missing: {path}"]
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return [row for row in csv.DictReader(handle) if row.get("reason") in PROPERTY_GAP_REASONS], []
    except Exception as exc:  # noqa: BLE001
        return [], [f"property gap CSV unreadable: {exc}"]


def update_text_issues(text: str) -> list[str]:
    text = text or ""
    issues: list[str] = []
    if not text.strip():
        issues.append("update_text_empty")
    marker_count = len(PROPERTY_UPDATE_MARKER_RE.findall(text))
    if marker_count != 1:
        issues.append(f"property_update_marker_count={marker_count}")
    heading_count = len(DATED_UPDATE_HEADING_RE.findall(text))
    if heading_count > 1:
        issues.append(f"dated_update_heading_count={heading_count}>1")
    if len(PROPERTY_UPDATES_HEADER_RE.findall(text)) > 0:
        issues.append("full_updates_header_present")
    if not LOFTY_RESERVE_SUMMARY_RE.search(text):
        issues.append("missing_lofty_reserve_summary")
    if not ECO_GL_SUMMARY_RE.search(text):
        issues.append("missing_eco_gl_column_e_summary")
    if len(text) > MAX_UPDATE_CHARS:
        issues.append(f"update_char_count={len(text)}>max={MAX_UPDATE_CHARS}")
    line_count = len(text.splitlines())
    if line_count > MAX_UPDATE_LINES:
        issues.append(f"update_line_count={line_count}>max={MAX_UPDATE_LINES}")
    return issues


def file_info(path: Path) -> dict[str, Any]:
    if not str(path):
        return {"path": "", "exists": False, "size": None, "sha256": ""}
    if not path.is_file():
        return {"path": str(path), "exists": False, "size": None, "sha256": ""}
    text = path.read_text(encoding="utf-8", errors="replace")
    return {"path": str(path), "exists": True, "size": path.stat().st_size, "sha256": sha256_text(text)}


def build_record(row: dict[str, str]) -> dict[str, Any]:
    updates_md = Path(row.get("updates_md") or "")
    candidate_raw = str(row.get("candidate_update_source") or "").strip()
    approval_target_raw = str(row.get("candidate_update_approval_target") or "").strip()
    financial_target_raw = str(row.get("candidate_financial_approval_target") or "").strip()
    candidate = Path(candidate_raw) if candidate_raw else None
    approval_target = Path(approval_target_raw) if approval_target_raw else None
    financial_target = Path(financial_target_raw) if financial_target_raw else None
    record: dict[str, Any] = {
        "property_name": row.get("property_name"),
        "lofty_property_id": row.get("lofty_property_id"),
        "gap_reason": row.get("reason"),
        "updates_md": str(updates_md),
        "candidate_update_source": candidate_raw,
        "candidate_update_approval_target": approval_target_raw,
        "candidate_financial_approval_target": financial_target_raw,
        "mutates_lofty_listing": False,
        "sends_owner_email": False,
    }
    issues: list[str] = []
    updates_md_missing = not updates_md.is_file()
    updates_md_empty = updates_md.is_file() and updates_md.stat().st_size == 0
    if updates_md.is_file() and updates_md.stat().st_size != 0:
        issues.append(f"updates_md_not_empty:size={updates_md.stat().st_size}")
    if not candidate or not candidate.is_file():
        issues.append("candidate_update_source_missing")
    if not approval_target:
        issues.append("candidate_update_approval_target_missing")
    if not financial_target or not financial_target.is_file():
        issues.append("candidate_financial_approval_target_missing")

    candidate_text = candidate.read_text(encoding="utf-8", errors="replace") if candidate and candidate.is_file() else ""
    candidate_issues = update_text_issues(candidate_text)
    approved_text = approval_target.read_text(encoding="utf-8", errors="replace") if approval_target and approval_target.is_file() else ""
    approved_issues = update_text_issues(approved_text) if approval_target and approval_target.is_file() else []

    record.update(
        {
            "updates_md_exists": updates_md.is_file(),
            "updates_md_size": updates_md.stat().st_size if updates_md.is_file() else None,
            "candidate_update_exists": bool(candidate and candidate.is_file()),
            "candidate_update_size": candidate.stat().st_size if candidate and candidate.is_file() else None,
            "candidate_update_sha256": sha256_text(candidate_text) if candidate and candidate.is_file() else "",
            "candidate_update_issues": candidate_issues,
            "approval_target_exists": bool(approval_target and approval_target.is_file()),
            "approval_target_size": approval_target.stat().st_size if approval_target and approval_target.is_file() else None,
            "approval_target_sha256": sha256_text(approved_text) if approval_target and approval_target.is_file() else "",
            "approval_target_issues": approved_issues,
            "financial_approval_target_exists": bool(financial_target and financial_target.is_file()),
            "financial_approval_target_size": financial_target.stat().st_size if financial_target and financial_target.is_file() else None,
        }
    )
    if issues:
        record.update({"status": "blocked", "reason": ";".join(issues)})
        return record
    if not updates_md_missing and not updates_md_empty:
        record.update({"status": "blocked", "reason": "updates_md_not_empty_or_missing"})
        return record
    if approval_target and approval_target.is_file() and approved_issues and not candidate_issues and candidate:
        record.update(
            {
                "status": "needs_update_approval_target_refresh",
                "reason": "existing approved update artifact is stale or incomplete; refresh from safe candidate only after current rent-roll/readiness approval",
                "source_for_backfill": str(candidate),
                "source_for_backfill_sha256": sha256_text(candidate_text),
                "approval_copy_command_requires_current_rent_roll_and_explicit_approval": shell_command(
                    ["bash", "-lc", f"cp -- {shlex.quote(str(candidate))} {shlex.quote(str(approval_target))}"]
                ),
            }
        )
        return record
    if approval_target and approval_target.is_file() and approved_issues:
        record.update({"status": "blocked_unsafe_approved_update", "reason": ";".join(approved_issues)})
        return record
    if (not approval_target or not approval_target.is_file()) and candidate_issues:
        record.update({"status": "blocked_unsafe_candidate_update", "reason": ";".join(candidate_issues)})
        return record

    if approval_target and approval_target.is_file():
        condition = "test ! -e" if updates_md_missing else "test ! -s"
        reason = (
            "canonical UPDATES.md is missing and approved monthly update artifact exists"
            if updates_md_missing
            else "canonical UPDATES.md is empty and approved monthly update artifact exists"
        )
        record.update(
            {
                "status": "ready_local_backfill_from_approved",
                "reason": reason,
                "source_for_backfill": str(approval_target),
                "source_for_backfill_sha256": sha256_text(approved_text),
                "local_backfill_command_requires_explicit_approval": shell_command(
                    ["bash", "-lc", f"{condition} {shlex.quote(str(updates_md))} && cp -- {shlex.quote(str(approval_target))} {shlex.quote(str(updates_md))}"]
                ),
            }
        )
        return record

    record.update(
        {
            "status": "needs_update_approval_target",
            "reason": "candidate update is safe but approval target is missing; approval remains gated by current rent-roll/readiness policy",
            "source_for_backfill": str(candidate),
            "source_for_backfill_sha256": sha256_text(candidate_text),
            "approval_copy_command_requires_current_rent_roll_and_explicit_approval": shell_command(
                ["bash", "-lc", f"test ! -e {shlex.quote(str(approval_target))} && cp -- {shlex.quote(str(candidate))} {shlex.quote(str(approval_target))}"]
            ),
        }
    )
    return record


def count_statuses(records: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        status = str(record.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def infer_run_month(records: list[dict[str, Any]]) -> str | None:
    months: set[str] = set()
    for record in records:
        for field in ("candidate_update_approval_target", "candidate_financial_approval_target", "candidate_update_source"):
            value = normalized_path_text(record.get(field))
            match = MONTH_RE.search(value)
            if match:
                months.add(match.group(1))
    return next(iter(months)) if len(months) == 1 else None


def write_markdown_packet(markdown_path: Path, report: dict[str, Any], records: list[dict[str, Any]]) -> None:
    status_counts = report.get("record_status_counts") or {}
    lines = [
        "# Lofty Empty UPDATES Backfill Queue",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Run month: {report.get('run_month') or 'unknown'}",
        f"- Status: {report.get('status')}",
        f"- Property count: {report.get('property_count')}",
        f"- Ready local backfill from approved: {report.get('ready_local_backfill_from_approved_count')}",
        f"- Needs update approval target: {report.get('needs_update_approval_target_count')}",
        f"- Blocked: {report.get('blocked_count')}",
        f"- Status counts: {json.dumps(status_counts, sort_keys=True)}",
        f"- Queue CSV: {report.get('queue_csv')}",
        f"- Local backfill script: {report.get('local_backfill_from_approved_commands_file')}",
        f"- Approval copy script: {report.get('approval_copy_commands_requires_current_rent_roll_and_explicit_approval_file')}",
        f"- Idempotency digest: {report.get('empty_updates_backfill_idempotency_digest')}",
        "",
        "## Guardrails",
        "",
        "- External mutation: none; this packet is local review evidence only.",
        "- Local backfill commands require explicit operator approval and only copy already-approved monthly update artifacts into empty or missing canonical UPDATES.md files.",
        "- Approval-copy commands require a current rent roll and explicit approval before creating monthly approved update artifacts.",
        "- This queue does not publish Lofty listing updates and does not send owner email.",
        "- This packet intentionally lists paths and statuses only; it does not copy full UPDATES.md bodies into review output.",
        "",
        "## Properties",
        "",
        "| Property | Lofty ID | Status | Gap | Approval target | Financial target | Reason |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for record in records:
        lines.append(
            "| "
            + " | ".join(
                markdown_escape(value)
                for value in (
                    record.get("property_name"),
                    record.get("lofty_property_id"),
                    record.get("status"),
                    record.get("gap_reason"),
                    record.get("candidate_update_approval_target"),
                    record.get("candidate_financial_approval_target"),
                    record.get("reason"),
                )
            )
            + " |"
        )
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_artifacts(report_path: Path, records: list[dict[str, Any]], digest: str) -> dict[str, Any]:
    artifact_dir = report_path.parent
    artifact_dir.mkdir(parents=True, exist_ok=True)
    base = report_path.stem
    csv_path = artifact_dir / f"{base}.csv"
    markdown_path = artifact_dir / f"{base}.md"
    approved_commands = artifact_dir / f"{base}.local-backfill-from-approved.requires-explicit-approval.sh"
    approval_commands = artifact_dir / f"{base}.approval-copy.requires-current-rent-roll-and-explicit-approval.sh"
    fieldnames = [
        "property_name",
        "lofty_property_id",
        "status",
        "reason",
        "updates_md",
        "updates_md_size",
        "candidate_update_source",
        "candidate_update_sha256",
        "candidate_update_approval_target",
        "approval_target_sha256",
        "candidate_financial_approval_target",
        "local_backfill_command_requires_explicit_approval",
        "approval_copy_command_requires_current_rent_roll_and_explicit_approval",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            writer.writerow({field: record.get(field, "") for field in fieldnames})

    approved_lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "# DO NOT RUN WITHOUT EXPLICIT OPERATOR APPROVAL.",
        "# Local Dropbox backfill only; does not publish Lofty listing updates and does not send owner email.",
        f"# empty_updates_backfill_idempotency_digest={digest}",
    ]
    approval_lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "# DO NOT RUN UNTIL RENT ROLL IS CURRENT AND OPERATOR APPROVES THESE CANDIDATES.",
        "# Copies safe review candidates to monthly approved artifacts; does not modify UPDATES.md, publish listings, or send email.",
        f"# empty_updates_backfill_idempotency_digest={digest}",
    ]
    approved_count = 0
    approval_count = 0
    for record in records:
        label = str(record.get("property_name") or record.get("lofty_property_id") or "property")
        local_command = str(record.get("local_backfill_command_requires_explicit_approval") or "")
        approval_command = str(record.get("approval_copy_command_requires_current_rent_roll_and_explicit_approval") or "")
        if local_command:
            approved_lines.extend(["", f"# {label}", local_command])
            approved_count += 1
        if approval_command:
            approval_lines.extend(["", f"# {label}", approval_command])
            approval_count += 1
    approved_commands.write_text("\n".join(approved_lines).rstrip() + "\n", encoding="utf-8")
    approval_commands.write_text("\n".join(approval_lines).rstrip() + "\n", encoding="utf-8")
    return {
        "queue_csv": str(csv_path),
        "queue_markdown": str(markdown_path),
        "local_backfill_from_approved_commands_file": str(approved_commands),
        "approval_copy_commands_requires_current_rent_roll_and_explicit_approval_file": str(approval_commands),
        "local_backfill_from_approved_command_count": approved_count,
        "approval_copy_command_requires_current_rent_roll_count": approval_count,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a review-only queue for active properties whose canonical UPDATES.md files are empty.")
    parser.add_argument("--property-gap-csv", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)

    rows, issues = read_gap_rows(args.property_gap_csv)
    records = [build_record(row) for row in rows]
    command_hygiene_records: list[dict[str, Any]] = []
    for record in records:
        hygiene_issues = command_hygiene_issues(record)
        if hygiene_issues:
            record["command_hygiene_issues"] = hygiene_issues
            command_hygiene_records.append(
                {
                    "property_name": record.get("property_name"),
                    "lofty_property_id": record.get("lofty_property_id"),
                    "status": record.get("status"),
                    "issues": hygiene_issues,
                }
            )
    if command_hygiene_records:
        issues.append(f"command_hygiene_issue_count={len(command_hygiene_records)}")
    manifest = [
        {
            "property_name": record.get("property_name"),
            "lofty_property_id": record.get("lofty_property_id"),
            "status": record.get("status"),
            "updates_md": record.get("updates_md"),
            "candidate_update_sha256": record.get("candidate_update_sha256"),
            "approval_target_sha256": record.get("approval_target_sha256"),
            "source_for_backfill_sha256": record.get("source_for_backfill_sha256"),
        }
        for record in records
    ]
    digest = stable_digest(manifest)
    status_counts = count_statuses(records)
    ready_count = status_counts.get("ready_local_backfill_from_approved", 0)
    approval_required_count = status_counts.get("needs_update_approval_target", 0) + status_counts.get("needs_update_approval_target_refresh", 0)
    blocked_count = sum(count for status, count in status_counts.items() if status.startswith("blocked"))
    artifacts = write_artifacts(args.report, records, digest)
    report = {
        "generated_at": iso_z(),
        "run_month": infer_run_month(records),
        "status": "failed" if issues else "review" if records else "ok",
        "issues": issues,
        "issue_count": len(issues),
        "mutates_dropbox_files": False,
        "mutates_lofty_listing": False,
        "sends_owner_email": False,
        "commands_require_explicit_approval": True,
        "approval_copy_requires_current_rent_roll": True,
        "property_gap_csv": str(args.property_gap_csv),
        "property_count": len(records),
        "ready_local_backfill_from_approved_count": ready_count,
        "needs_update_approval_target_count": approval_required_count,
        "blocked_count": blocked_count,
        "command_hygiene_issue_count": len(command_hygiene_records),
        "command_hygiene_records_bounded": command_hygiene_records[:25],
        "record_status_counts": status_counts,
        "empty_updates_backfill_idempotency_digest": digest,
        "manifest": manifest,
        "next_action": (
            "Review queue_csv. Run local-backfill commands only for already-approved update targets; "
            "run approval-copy commands only after rent roll is current and candidates are explicitly approved."
            if records
            else "No empty UPDATES.md gaps found in property gap CSV."
        ),
        **artifacts,
        "records": records,
    }
    write_markdown_packet(Path(str(report["queue_markdown"])), report, records)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "issue_count", "property_count", "ready_local_backfill_from_approved_count", "needs_update_approval_target_count", "blocked_count")}, indent=2, sort_keys=True))
    return 1 if issues else 2 if report["status"] == "review" else 0


if __name__ == "__main__":
    raise SystemExit(main())
