#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


APPROVAL_ENV_VAR = "LOFTY_UNREVIEWED_FINANCIAL_APPROVAL_QUARANTINE_APPROVED"
APPROVAL_REQUIRED_VALUE = "1"
DIGEST_ENV_VAR = "LOFTY_UNREVIEWED_FINANCIAL_APPROVAL_QUARANTINE_DIGEST"
FINANCIALS_DIR_NAME = "00 - README & Property Snapshot"


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def quarantine_target(source: Path) -> Path:
    if source.name.endswith("-approved.md"):
        return source.with_name(source.name.removesuffix("-approved.md") + "-review-needed.md")
    if source.name.endswith(".approved.md"):
        return source.with_name(source.name.removesuffix(".approved.md") + ".review-needed.md")
    return source.with_name(source.name + ".review-needed")


def shell_command(source: Path, target: Path, digest: str) -> str:
    return " ".join(
        [
            f'test "${{{APPROVAL_ENV_VAR}:-}}" = {shlex.quote(APPROVAL_REQUIRED_VALUE)}',
            "&&",
            f'test "${{{DIGEST_ENV_VAR}:-}}" = {shlex.quote(digest)}',
            "&&",
            "test",
            "-f",
            shlex.quote(str(source)),
            "&&",
            "test",
            "!",
            "-e",
            shlex.quote(str(target)),
            "&&",
            "mv",
            "--",
            shlex.quote(str(source)),
            shlex.quote(str(target)),
        ]
    )


def command_digest(entries: list[dict[str, Any]]) -> str:
    stable = [
        {
            "approved_draft": entry.get("approved_draft"),
            "target": entry.get("target"),
            "source_sha256": entry.get("source_sha256"),
        }
        for entry in entries
    ]
    return sha256_text(json.dumps(stable, sort_keys=True, separators=(",", ":")))


def build_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for index, record in enumerate(payload.get("records") or []):
        if not isinstance(record, dict):
            continue
        financials = record.get("financials") if isinstance(record.get("financials"), dict) else {}
        if financials.get("status") != "approved_financials_unreviewed":
            continue
        source_value = str(financials.get("approved_draft") or "")
        source = Path(source_value).expanduser().resolve() if source_value else None
        target = quarantine_target(source) if source else None
        issues = []
        source_exists = bool(source and source.is_file())
        target_exists = bool(target and target.exists())
        source_sha = file_sha256(source) if source_exists and source else None
        if not source:
            issues.append("missing_approved_draft_path")
        elif FINANCIALS_DIR_NAME not in source.parts:
            issues.append("noncanonical_financials_dir")
        if not source_exists:
            issues.append("approved_draft_missing")
        if target_exists:
            issues.append("quarantine_target_exists")
        entries.append(
            {
                "index": index,
                "property_name": Path(str(record.get("property_path") or "")).name,
                "property_path": record.get("property_path"),
                "approved_draft": str(source) if source else None,
                "target": str(target) if target else None,
                "source_exists": source_exists,
                "target_exists": target_exists,
                "source_sha256": source_sha,
                "quality_issues": financials.get("approved_financials_quality_issues") or [],
                "issues": issues,
                "status": "ready_to_quarantine" if not issues else "review",
            }
        )
    return entries


def write_commands(path: Path, entries: list[dict[str, Any]], digest: str) -> int:
    ready_entries = [entry for entry in entries if entry.get("status") == "ready_to_quarantine"]
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        f"# {APPROVAL_ENV_VAR}={APPROVAL_REQUIRED_VALUE} is required.",
        f"# {DIGEST_ENV_VAR}={digest} is required.",
        "# This renames generated/unreviewed FINANCIALS approved snapshots to review-needed files.",
        "# No email, listing, deletion, source-ledger, or external-message operations.",
        "",
    ]
    if not ready_entries:
        lines.extend(
            [
                "# No unreviewed generated FINANCIALS approved snapshots require quarantine.",
                "exit 0",
            ]
        )
    count = 0
    for entry in ready_entries:
        source = Path(str(entry["approved_draft"]))
        target = Path(str(entry["target"]))
        lines.append(f"# {entry.get('property_name')}")
        lines.append(shell_command(source, target, digest))
        lines.append("")
        count += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return count


def apply_entries(entries: list[dict[str, Any]], digest: str) -> list[dict[str, Any]]:
    results = []
    approval_ok = os.environ.get(APPROVAL_ENV_VAR) == APPROVAL_REQUIRED_VALUE
    digest_ok = os.environ.get(DIGEST_ENV_VAR) == digest
    for entry in entries:
        result = {
            "property_name": entry.get("property_name"),
            "approved_draft": entry.get("approved_draft"),
            "target": entry.get("target"),
            "status": "skipped",
        }
        if entry.get("status") != "ready_to_quarantine":
            result["status"] = "skipped_not_ready"
        elif not approval_ok:
            result["status"] = "blocked_missing_approval"
        elif not digest_ok:
            result["status"] = "blocked_digest_mismatch"
        else:
            source = Path(str(entry["approved_draft"]))
            target = Path(str(entry["target"]))
            if not source.is_file():
                result["status"] = "skipped_source_missing"
            elif target.exists():
                result["status"] = "skipped_target_exists"
            else:
                source.rename(target)
                result["status"] = "quarantined"
        results.append(result)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan or apply quarantine of generated/unreviewed approved FINANCIALS snapshots.")
    parser.add_argument("--guarded-apply-report", type=Path, default=Path("reports/baselane_financials_monthly_guarded_apply.json"))
    parser.add_argument("--report", type=Path, default=Path("reports/lofty_unreviewed_financial_approval_quarantine.json"))
    parser.add_argument(
        "--commands-file",
        type=Path,
        default=Path("reports/lofty_unreviewed_financial_approval_quarantine.requires-explicit-approval.sh"),
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    issues = []
    entries: list[dict[str, Any]] = []
    if not args.guarded_apply_report.is_file():
        issues.append({"code": "guarded_apply_report_missing", "detail": str(args.guarded_apply_report)})
    else:
        entries = build_entries(read_json(args.guarded_apply_report))
    ready_entries = [entry for entry in entries if entry.get("status") == "ready_to_quarantine"]
    review_entries = [entry for entry in entries if entry.get("status") != "ready_to_quarantine"]
    digest = command_digest(ready_entries)
    command_count = write_commands(args.commands_file, ready_entries, digest)
    apply_results = apply_entries(entries, digest) if args.apply else []
    quarantined_count = sum(1 for item in apply_results if item.get("status") == "quarantined")
    blocked_apply_count = sum(1 for item in apply_results if str(item.get("status") or "").startswith("blocked_"))
    status = "ok"
    if issues or review_entries or blocked_apply_count:
        status = "review"
    if ready_entries and not args.apply:
        status = "review"
    if args.apply and blocked_apply_count:
        status = "failed"

    report = {
        "generated_at": iso_z(),
        "status": status,
        "source_report": str(args.guarded_apply_report),
        "mutates_dropbox_files": args.apply,
        "mutates_lofty_listing": False,
        "sends_owner_email": False,
        "apply": args.apply,
        "approval_env_var": APPROVAL_ENV_VAR,
        "approval_required_value": APPROVAL_REQUIRED_VALUE,
        "digest_env_var": DIGEST_ENV_VAR,
        "digest_required_value": digest,
        "commands_file": str(args.commands_file),
        "command_count": command_count,
        "unreviewed_approved_financial_count": len(entries),
        "ready_to_quarantine_count": len(ready_entries),
        "review_count": len(review_entries),
        "quarantined_count": quarantined_count,
        "apply_results": apply_results,
        "issues": issues + [
            {"code": "quarantine_entry_not_ready", "detail": f"{entry.get('property_name')}:{','.join(entry.get('issues') or [])}"}
            for entry in review_entries
        ],
        "entries": entries,
    }
    write_json(args.report, report)
    print(json.dumps({key: report[key] for key in ("status", "unreviewed_approved_financial_count", "ready_to_quarantine_count", "command_count")}, indent=2))
    return 0 if status == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
