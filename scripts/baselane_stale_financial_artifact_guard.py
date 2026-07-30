#!/usr/bin/env python3
"""Fail closed when legacy financial artifacts remain in Dropbox property trees."""

import argparse
import datetime as dt
import json
import os
from pathlib import Path

try:
    from .baselane_report_integrity_guard import validate_stale_financial_artifacts
except ImportError:
    from baselane_report_integrity_guard import validate_stale_financial_artifacts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("REAL_ESTATE_ROOT", "/mnt/c/Users/digit/Dropbox/Real Estate")),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path(
            os.environ.get(
                "BASELANE_STALE_FINANCIAL_ARTIFACT_GUARD_FILE",
                str(Path(__file__).resolve().parents[1] / "reports" / "baselane_stale_financial_artifact_guard.json"),
            )
        ),
    )
    args = parser.parse_args(argv)
    root = args.root
    generated_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    issues = []
    scan_error = None
    try:
        issues = validate_stale_financial_artifacts(root)
    except Exception as exc:  # conservative: an unreadable source cannot pass the gate
        scan_error = f"{type(exc).__name__}: {exc}"
        issues = [{"code": "stale_financial_artifact_scan_failed", "detail": scan_error}]

    payload = {
        "job": "baselane-stale-financial-artifact-guard",
        "generated_at": generated_at,
        "status": "ok" if not issues and root.exists() else "review",
        "real_estate_root": str(root),
        "issue_count": len(issues),
        "issues": issues,
        "mutation_attempted": False,
        "policy": {
            "reject_legacy_reconciliation_markdown": True,
            "reject_2026_01_2026_02_pnl_exports": True,
            "scan_scope": "property roots and canonical Public/07 - P&L & Owner Statements branches",
        },
    }
    if not root.exists() and not scan_error:
        payload["issues"] = [{"code": "real_estate_root_missing", "detail": str(root)}]
        payload["issue_count"] = 1
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": payload["status"], "issue_count": payload["issue_count"], "report": str(args.report)}))
    return 0 if payload["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
