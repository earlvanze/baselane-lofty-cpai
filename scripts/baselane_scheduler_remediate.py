#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from baselane_scheduler_audit import (
    SCHEDULER_REMEDIATION_APPROVAL_ENV,
    SCHEDULER_REMEDIATION_DIGEST_ENV,
    remediation_digest,
)


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"status": "unreadable", "error": str(exc), "path": str(path)}


def jobs_list(payload: Any) -> list[dict[str, Any]]:
    jobs = payload.get("jobs") if isinstance(payload, dict) else payload
    if not isinstance(jobs, list):
        return []
    return [job for job in jobs if isinstance(job, dict)]


def disable_jobs(payload: Any, job_ids: set[str]) -> tuple[int, list[str], list[str]]:
    changed = 0
    found: list[str] = []
    already_disabled: list[str] = []
    for job in jobs_list(payload):
        job_id = str(job.get("id") or "")
        if job_id not in job_ids:
            continue
        found.append(job_id)
        if job.get("enabled") is False:
            already_disabled.append(job_id)
            continue
        job["enabled"] = False
        changed += 1
    return changed, found, already_disabled


def build_report(*, jobs_json: Path, disable_job_ids: list[str], expected_digest: str, apply: bool) -> tuple[dict[str, Any], Any | None]:
    payload = read_json(jobs_json)
    if payload.get("status") == "unreadable":
        return (
            {
                "generated_at": iso_z(),
                "status": "failed",
                "issue_count": 1,
                "issues": [f"jobs_json_unreadable:{jobs_json}:{payload.get('error')}"],
                "jobs_json": str(jobs_json),
                "apply": apply,
            },
            None,
        )
    records = [
        {
            "action": "disable_openclaw_job",
            "requires_explicit_approval": True,
            "source": str(jobs_json),
            "job_id": job_id,
        }
        for job_id in disable_job_ids
    ]
    digest = remediation_digest(records)
    issues: list[str] = []
    if expected_digest and digest != expected_digest:
        issues.append(f"digest_mismatch:{digest}!={expected_digest}")
    if apply and os.environ.get(SCHEDULER_REMEDIATION_APPROVAL_ENV) != "1":
        issues.append(f"{SCHEDULER_REMEDIATION_APPROVAL_ENV}=1 required")
    if apply and expected_digest and os.environ.get(SCHEDULER_REMEDIATION_DIGEST_ENV) != expected_digest:
        issues.append(f"{SCHEDULER_REMEDIATION_DIGEST_ENV}={expected_digest} required")
    before_enabled = sorted(
        str(job.get("id") or "")
        for job in jobs_list(payload)
        if str(job.get("id") or "") in set(disable_job_ids) and job.get("enabled") is not False
    )
    changed_count, found, already_disabled = disable_jobs(payload, set(disable_job_ids))
    missing = sorted(set(disable_job_ids) - set(found))
    if missing:
        issues.append(f"job_id_missing:{','.join(missing)}")
    if issues:
        status = "review"
        changed_count = 0
        mutated_payload = None
    elif apply:
        status = "ok"
        mutated_payload = payload
    else:
        status = "ok"
        mutated_payload = None
    return (
        {
            "generated_at": iso_z(),
            "status": status,
            "issue_count": len(issues),
            "issues": issues,
            "jobs_json": str(jobs_json),
            "apply": apply,
            "mutates_scheduler": apply and not issues,
            "requires_explicit_approval": True,
            "approval_env_var": SCHEDULER_REMEDIATION_APPROVAL_ENV,
            "approval_digest_env_var": SCHEDULER_REMEDIATION_DIGEST_ENV,
            "expected_digest": expected_digest,
            "computed_digest": digest,
            "disable_job_ids": disable_job_ids,
            "found_job_ids": sorted(found),
            "missing_job_ids": missing,
            "already_disabled_job_ids": sorted(already_disabled),
            "enabled_before_job_ids": before_enabled,
            "changed_count": changed_count if apply and not issues else 0,
        },
        mutated_payload,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Approval-gated OpenClaw scheduler remediation for Baselane audit findings.")
    parser.add_argument("--jobs-json", required=True, type=Path)
    parser.add_argument("--disable-job-id", action="append", default=[])
    parser.add_argument("--expected-digest", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", default="reports/baselane_scheduler_remediation_report.json", type=Path)
    args = parser.parse_args()

    jobs_json = args.jobs_json.expanduser()
    report, mutated_payload = build_report(
        jobs_json=jobs_json,
        disable_job_ids=list(dict.fromkeys(args.disable_job_id)),
        expected_digest=args.expected_digest,
        apply=args.apply,
    )
    if mutated_payload is not None:
        backup = jobs_json.with_suffix(jobs_json.suffix + "." + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".bak")
        shutil.copy2(jobs_json, backup)
        jobs_json.write_text(json.dumps(mutated_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        report["backup_path"] = str(backup)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report.get(key) for key in ("status", "issue_count", "changed_count", "apply")}, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
