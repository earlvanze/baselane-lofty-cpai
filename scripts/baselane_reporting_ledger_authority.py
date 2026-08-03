#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from baselane_weekly_unprocessed_report import exact_deduped_ledger_rows


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stable_digest(payload: object) -> str:
    material = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        if not fieldnames:
            raise ValueError(f"Ledger has no CSV header: {path}")
        return fieldnames, list(reader)


def csv_digest(path: Path) -> tuple[str, int]:
    fieldnames, rows = read_csv(path)
    return stable_digest({"fieldnames": fieldnames, "rows": rows}), len(rows)


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Report must contain a JSON object: {path}")
    return payload


def same_path(left: object, right: Path) -> bool:
    if not left:
        return False
    return Path(str(left)).expanduser().resolve() == right.expanduser().resolve()


def run_refresh(root: Path, raw_ledger: Path, run_month: str) -> list[dict[str, Any]]:
    reports = root / "reports"
    scripts = root / "scripts"
    commands = [
        [
            sys.executable,
            str(scripts / "baselane_weekly_unprocessed_report.py"),
            "--ledger",
            str(raw_ledger),
            "--out-json",
            str(reports / "baselane_monthly_reporting_raw_duplicate_report.json"),
            "--out-csv",
            str(reports / "baselane_monthly_reporting_raw_duplicate_candidates.csv"),
            "--state-file",
            str(scripts / ".baselane_monthly_reporting_raw_duplicate_state.json"),
            "--duplicate-allowlist",
            str(scripts / ".baselane_weekly_duplicate_allowlist.json"),
            "--deduped-ledger-out",
            str(reports / "baselane_weekly_deduped_reporting_ledger.csv"),
        ],
        [
            sys.executable,
            str(scripts / "baselane_ecogl_apply_safe_actions.py"),
            "--ledger",
            str(reports / "baselane_weekly_deduped_reporting_ledger.csv"),
            "--out-ledger",
            str(reports / "baselane_weekly_safe_category_reporting_ledger.csv"),
            "--report",
            str(reports / "baselane_ecogl_safe_category_apply_report.json"),
            "--actions-csv",
            str(reports / "baselane_ecogl_safe_category_apply_actions.csv"),
            "--markdown",
            str(reports / "baselane_ecogl_safe_category_apply_report.md"),
            "--historical-apply-month",
            run_month,
            "--apply",
        ],
        [
            sys.executable,
            str(scripts / "baselane_quarantine_first_day_pm_fee_rows.py"),
            "--ledger",
            str(reports / "baselane_weekly_safe_category_reporting_ledger.csv"),
            "--out-ledger",
            str(reports / "baselane_weekly_clean_reporting_ledger.csv"),
            "--report",
            str(reports / "baselane_first_day_pm_fee_quarantine_report.json"),
            "--quarantine-csv",
            str(reports / "baselane_first_day_pm_fee_quarantine_rows.csv"),
            "--markdown",
            str(reports / "baselane_first_day_pm_fee_quarantine_report.md"),
            "--all-months",
        ],
        [
            sys.executable,
            str(scripts / "baselane_quarantine_no_dao_mortgage_rows.py"),
            "--ledger",
            str(reports / "baselane_weekly_clean_reporting_ledger.csv"),
            "--out-ledger",
            str(reports / "baselane_weekly_no_dao_mortgage_clean_reporting_ledger.csv"),
            "--report",
            str(reports / "baselane_no_dao_mortgage_reporting_quarantine.json"),
            "--quarantine-csv",
            str(reports / "baselane_no_dao_mortgage_reporting_quarantine_rows.csv"),
            "--markdown",
            str(reports / "baselane_no_dao_mortgage_reporting_quarantine.md"),
        ],
    ]
    results: list[dict[str, Any]] = []
    for command in commands:
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
        result = {
            "script": Path(command[1]).name,
            "return_code": completed.returncode,
            "stdout_tail": completed.stdout[-1000:],
            "stderr_tail": completed.stderr[-1000:],
        }
        results.append(result)
        if completed.returncode != 0:
            break
    return results


def audit_authority(
    *,
    root: Path,
    raw_ledger: Path,
    reporting_ledger: Path,
    refresh_attempted: bool,
    refresh_results: list[dict[str, Any]],
) -> dict[str, Any]:
    reports = root / "reports"
    paths = {
        "deduped_ledger": reports / "baselane_weekly_deduped_reporting_ledger.csv",
        "safe_ledger": reports / "baselane_weekly_safe_category_reporting_ledger.csv",
        "pm_clean_ledger": reports / "baselane_weekly_clean_reporting_ledger.csv",
        "reporting_ledger": reporting_ledger,
        "dedupe_report": reports / "baselane_monthly_reporting_raw_duplicate_report.json",
        "safe_report": reports / "baselane_ecogl_safe_category_apply_report.json",
        "pm_report": reports / "baselane_first_day_pm_fee_quarantine_report.json",
        "mortgage_report": reports / "baselane_no_dao_mortgage_reporting_quarantine.json",
    }
    issues: list[str] = []
    for label, path in {"raw_ledger": raw_ledger, **paths}.items():
        if not path.is_file():
            issues.append(f"missing {label}: {path}")

    refresh_failures = [result for result in refresh_results if int(result.get("return_code") or 0) != 0]
    if refresh_failures:
        issues.append(f"reporting ledger refresh failed at {refresh_failures[0].get('script')}")

    report_payloads: dict[str, dict[str, Any]] = {}
    if not issues:
        for label in ("dedupe_report", "safe_report", "pm_report", "mortgage_report"):
            try:
                report_payloads[label] = read_json(paths[label])
            except Exception as exc:  # noqa: BLE001
                issues.append(f"unreadable {label}: {exc}")

    digests: dict[str, str] = {}
    row_counts: dict[str, int] = {}
    expected_deduped_digest = ""
    expected_deduped_row_count = 0
    exact_duplicate_extra_row_count = 0
    if not issues:
        try:
            raw_fieldnames, raw_rows = read_csv(raw_ledger)
            expected_rows, _duplicate_key_count, exact_duplicate_extra_row_count = exact_deduped_ledger_rows(
                raw_rows,
                raw_fieldnames,
            )
            expected_deduped_digest = stable_digest({"fieldnames": raw_fieldnames, "rows": expected_rows})
            expected_deduped_row_count = len(expected_rows)
            row_counts["raw"] = len(raw_rows)
            for label in ("deduped_ledger", "safe_ledger", "pm_clean_ledger", "reporting_ledger"):
                digests[label], row_counts[label] = csv_digest(paths[label])
        except Exception as exc:  # noqa: BLE001
            issues.append(f"unable to verify reporting ledger chain: {exc}")

    if not issues:
        dedupe = report_payloads["dedupe_report"]
        safe = report_payloads["safe_report"]
        pm = report_payloads["pm_report"]
        mortgage = report_payloads["mortgage_report"]

        checks = [
            (same_path(dedupe.get("ledger"), raw_ledger), "dedupe report raw ledger path mismatch"),
            (same_path(dedupe.get("deduped_reporting_ledger"), paths["deduped_ledger"]), "dedupe output path mismatch"),
            (int(dedupe.get("ledger_rows") or -1) == row_counts["raw"], "dedupe raw row count mismatch"),
            (
                int(dedupe.get("deduped_reporting_ledger_row_count") or -1) == expected_deduped_row_count,
                "dedupe output row count mismatch",
            ),
            (
                int(dedupe.get("exact_duplicate_extra_row_count") or -1) == exact_duplicate_extra_row_count,
                "dedupe removed-row count mismatch",
            ),
            (digests["deduped_ledger"] == expected_deduped_digest, "deduped ledger is stale or not derived from current raw ledger"),
            (safe.get("status") == "ok" and safe.get("mode") == "apply" and safe.get("output_written") is True, "safe-category report is not an applied success"),
            (same_path(safe.get("ledger"), paths["deduped_ledger"]), "safe-category input path mismatch"),
            (same_path(safe.get("out_ledger"), paths["safe_ledger"]), "safe-category output path mismatch"),
            (safe.get("input_digest") == digests["deduped_ledger"], "safe-category input digest mismatch"),
            (safe.get("output_digest") == digests["safe_ledger"], "safe-category output digest mismatch"),
            (pm.get("status") == "ok" and pm.get("mode") == "apply" and pm.get("output_written") is True, "PM-fee quarantine report is not an applied success"),
            (pm.get("reporting_output_clean") is True, "PM-fee reporting output is not clean"),
            (int(pm.get("remaining_first_day_pm_fee_count") or 0) == 0, "PM-fee quarantine left matching rows"),
            (same_path(pm.get("ledger_csv"), paths["safe_ledger"]), "PM-fee quarantine input path mismatch"),
            (same_path(pm.get("out_ledger"), paths["pm_clean_ledger"]), "PM-fee quarantine output path mismatch"),
            (pm.get("input_digest") == digests["safe_ledger"], "PM-fee quarantine input digest mismatch"),
            (pm.get("output_digest") == digests["pm_clean_ledger"], "PM-fee quarantine output digest mismatch"),
            (mortgage.get("status") == "ok" and mortgage.get("mode") == "apply" and mortgage.get("output_written") is True, "no-DAO mortgage quarantine report is not an applied success"),
            (mortgage.get("reporting_output_clean") is True, "no-DAO mortgage reporting output is not clean"),
            (int(mortgage.get("remaining_no_dao_mortgage_row_count") or 0) == 0, "no-DAO mortgage quarantine left matching rows"),
            (same_path(mortgage.get("ledger_csv"), paths["pm_clean_ledger"]), "no-DAO mortgage input path mismatch"),
            (same_path(mortgage.get("out_ledger"), paths["reporting_ledger"]), "no-DAO mortgage output path mismatch"),
            (mortgage.get("input_digest") == digests["pm_clean_ledger"], "no-DAO mortgage input digest mismatch"),
            (mortgage.get("output_digest") == digests["reporting_ledger"], "no-DAO mortgage output digest mismatch"),
        ]
        issues.extend(message for passed, message in checks if not passed)

    raw_sha256 = file_sha256(raw_ledger) if raw_ledger.is_file() else None
    reporting_sha256 = file_sha256(reporting_ledger) if reporting_ledger.is_file() else None
    authority_material = {
        "raw_sha256": raw_sha256,
        "reporting_sha256": reporting_sha256,
        "digests": digests,
        "row_counts": row_counts,
    }
    return {
        "generated_at": iso_z(),
        "status": "ok" if not issues else "review",
        "raw_ledger": str(raw_ledger),
        "raw_ledger_sha256": raw_sha256,
        "reporting_ledger": str(reporting_ledger),
        "reporting_ledger_sha256": reporting_sha256,
        "refresh_attempted": refresh_attempted,
        "refresh_results": refresh_results,
        "raw_source_mutated": False,
        "raw_row_count": row_counts.get("raw"),
        "reporting_row_count": row_counts.get("reporting_ledger"),
        "exact_duplicate_extra_row_count": exact_duplicate_extra_row_count,
        "pm_fee_quarantined_row_count": report_payloads.get("pm_report", {}).get("quarantined_row_count"),
        "no_dao_mortgage_quarantined_row_count": report_payloads.get("mortgage_report", {}).get("quarantined_row_count"),
        "chain_digests": digests,
        "authority_digest": stable_digest(authority_material),
        "issue_count": len(issues),
        "issues": issues,
        "policy": "Monthly source cash uses a freshly derived exact-row-deduped, safe-category, obsolete-PM-fee-clean, no-DAO-mortgage-clean reporting ledger. Raw Baselane remains the upstream evidence source.",
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh and verify the canonical derived ECO GL reporting ledger.")
    parser.add_argument("--root", type=Path, default=Path(__file__).absolute().parents[1])
    parser.add_argument(
        "--raw-ledger",
        type=Path,
        default=Path("/mnt/c/Users/digit/Dropbox/Projects/assetrail/ECO Systems General Ledger.csv"),
    )
    parser.add_argument("--month", required=True)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--reporting-ledger", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)

    root = args.root.expanduser().resolve()
    raw_ledger = args.raw_ledger.expanduser().resolve()
    reporting_ledger = (
        args.reporting_ledger.expanduser().resolve()
        if args.reporting_ledger
        else root / "reports" / "baselane_weekly_no_dao_mortgage_clean_reporting_ledger.csv"
    )
    report_path = (
        args.report.expanduser().resolve()
        if args.report
        else root / "reports" / "baselane_monthly_reporting_ledger_authority.json"
    )

    refresh_results: list[dict[str, Any]] = []
    if args.refresh:
        refresh_results = run_refresh(root, raw_ledger, args.month)
    report = audit_authority(
        root=root,
        raw_ledger=raw_ledger,
        reporting_ledger=reporting_ledger,
        refresh_attempted=args.refresh,
        refresh_results=refresh_results,
    )
    write_report(report_path, report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "issue_count": report["issue_count"],
                "reporting_ledger": report["reporting_ledger"],
                "authority_digest": report["authority_digest"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
