#!/usr/bin/env python3
"""Regenerate canonical property ledgers from one verified reporting ledger."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import split_ledger_public_financials as split


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected an object in {path}")
    return payload


def same_path(left: str | Path, right: str | Path) -> bool:
    return Path(left).expanduser().resolve(strict=False) == Path(right).expanduser().resolve(strict=False)


def report_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": report.get("status"),
        "issue_count": int(report.get("issue_count") or 0),
        "source": report.get("source"),
        "source_sha256": report.get("source_sha256"),
        "total_row_count": int(report.get("total_row_count") or 0),
        "deduped_row_count": int(report.get("deduped_row_count") or 0),
        "exact_duplicate_extra_row_count": int(report.get("exact_duplicate_extra_row_count") or 0),
        "planned_write_count": int(report.get("planned_write_count") or 0),
        "planned_row_count": int(report.get("planned_row_count") or 0),
        "output_current_count": int(report.get("output_current_count") or 0),
        "output_missing_count": int(report.get("output_missing_count") or 0),
        "output_stale_count": int(report.get("output_stale_count") or 0),
        "output_unreadable_count": int(report.get("output_unreadable_count") or 0),
        "output_mismatch_count": int(report.get("output_mismatch_count") or 0),
        "unresolved_property_count": int(report.get("unresolved_property_count") or 0),
        "deferred_acquisition_property_count": int(report.get("deferred_acquisition_property_count") or 0),
        "excluded_write_skipped_count": int(report.get("excluded_write_skipped_count") or 0),
        "eco_company_revenue_excluded_row_count": int(report.get("eco_company_revenue_excluded_row_count") or 0),
        "citadel_statement_split_applied_count": int(report.get("citadel_statement_split_applied_count") or 0),
        "write_attempted": bool(report.get("write_attempted")),
        "delete_attempted": bool(report.get("delete_attempted")),
    }


def split_report_is_clean(report: dict[str, Any]) -> bool:
    return (
        report.get("status") in {"ok", split.STATUS_OK}
        and report.get("classification") in {None, "ok"}
        and int(report.get("issue_count") or 0) == 0
    )


def plan_digest(report: dict[str, Any]) -> str:
    canonical = json.dumps(
        {
            "source": report.get("source"),
            "source_sha256": report.get("source_sha256"),
            "planned_write_count": report.get("planned_write_count"),
            "planned_row_count": report.get("planned_row_count"),
            "output_plan_digest": report.get("output_plan_digest"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def authority_issues(authority_path: Path | None, source: Path, source_sha256: str) -> list[str]:
    if authority_path is None:
        return []
    if not authority_path.is_file():
        return [f"authority report missing: {authority_path}"]
    try:
        authority = load_json(authority_path)
    except Exception as exc:  # noqa: BLE001
        return [f"authority report unreadable: {exc}"]

    issues: list[str] = []
    if authority.get("status") != "ok" or int(authority.get("issue_count") or 0):
        issues.append("reporting-ledger authority is not clean")
    if not same_path(str(authority.get("reporting_ledger") or ""), source):
        issues.append("authority reporting-ledger path does not match the requested source")
    if authority.get("reporting_ledger_sha256") != source_sha256:
        issues.append("authority reporting-ledger digest does not match the requested source")
    return issues


def build_refresh_report(
    *,
    source: Path,
    real_estate_root: Path,
    authority_report: Path | None,
    split_report: Path,
    apply: bool,
) -> dict[str, Any]:
    issues: list[str] = []
    if not source.is_file():
        issues.append(f"reporting ledger missing: {source}")
    if not real_estate_root.is_dir():
        issues.append(f"real-estate root missing: {real_estate_root}")

    source_sha256 = sha256_file(source) if source.is_file() else None
    if source_sha256:
        issues.extend(authority_issues(authority_report, source, source_sha256))

    preview: dict[str, Any] = {}
    digest = None
    if not issues:
        preview = split.build_report(
            source,
            real_estate_root,
            require_current_outputs=False,
        )
        digest = plan_digest(preview)
        if not split_report_is_clean(preview):
            issues.append("property-ledger split preview is not clean")
        if preview.get("source_sha256") != source_sha256:
            issues.append("property-ledger split preview source digest changed")
        if int(preview.get("unresolved_property_count") or 0):
            issues.append("property-ledger split preview contains unresolved properties")

    final: dict[str, Any] = {}
    apply_attempted = False
    if apply and not issues:
        apply_attempted = True
        split.main(
            [
                "--source",
                str(source),
                "--real-estate-base",
                str(real_estate_root),
                "--report",
                str(split_report),
            ]
        )
        final = load_json(split_report)
        if not split_report_is_clean(final):
            issues.append("applied property-ledger split did not verify cleanly")
        if final.get("source_sha256") != source_sha256:
            issues.append("applied property-ledger split source digest changed")
        if int(final.get("output_mismatch_count") or 0):
            issues.append("canonical property ledgers do not match the verified reporting ledger")
        if not final.get("write_attempted"):
            issues.append("applied property-ledger split did not record a write attempt")

    return {
        "generated_at": iso_z(),
        "status": "review" if issues else ("ok" if apply else "ok_preview"),
        "issue_count": len(issues),
        "issues": issues,
        "mode": "apply" if apply else "preview",
        "source": str(source),
        "source_sha256": source_sha256,
        "real_estate_root": str(real_estate_root),
        "authority_report": str(authority_report) if authority_report else None,
        "authority_verified": bool(authority_report) and not authority_issues(authority_report, source, source_sha256 or ""),
        "plan_digest": digest,
        "preview": report_summary(preview) if preview else {},
        "apply_requested": apply,
        "apply_attempted": apply_attempted,
        "split_report": str(split_report),
        "final": report_summary(final) if final else {},
        "raw_source_mutated": False,
        "property_ledger_write_attempted": apply_attempted,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--real-estate-root", type=Path, required=True)
    parser.add_argument("--authority-report", type=Path)
    parser.add_argument("--split-report", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = build_refresh_report(
            source=args.source,
            real_estate_root=args.real_estate_root,
            authority_report=args.authority_report,
            split_report=args.split_report,
            apply=args.apply,
        )
    except Exception as exc:  # noqa: BLE001
        report = {
            "generated_at": iso_z(),
            "status": "review",
            "issue_count": 1,
            "issues": [str(exc)],
            "mode": "apply" if args.apply else "preview",
            "source": str(args.source),
            "real_estate_root": str(args.real_estate_root),
            "authority_report": str(args.authority_report) if args.authority_report else None,
            "split_report": str(args.split_report),
            "apply_requested": args.apply,
            "apply_attempted": False,
            "raw_source_mutated": False,
        }
    write_json(args.report, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] in {"ok", "ok_preview"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
