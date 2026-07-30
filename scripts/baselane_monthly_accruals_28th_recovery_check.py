#!/usr/bin/env python3
"""Detect a missed current-month 28th Baselane accrual run without retrying it."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def build_report(*, run_month: str, current_date: date, accrual_report: Path) -> dict:
    report = read_json(accrual_report)
    due = current_date.strftime("%Y-%m") == run_month and current_date.day >= 28
    completed = report.get("status") == "ok" and report.get("run_month") == run_month

    payload = {
        "job": "baselane-monthly-accruals-28th-recovery-check",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "run_month": run_month,
        "current_local_date": current_date.isoformat(),
        "accrual_report": str(accrual_report),
        "accrual_report_status": report.get("status"),
        "accrual_report_run_month": report.get("run_month"),
        "external_mutation_attempted": False,
    }
    if not due:
        payload.update(status="not_due", reason="current_month_28th_window_not_reached")
    elif completed:
        payload.update(status="ok", reason="current_month_28th_accrual_report_complete")
    else:
        payload.update(
            status="review",
            reason="current_month_28th_accrual_report_missing_or_incomplete",
            next_action=(
                "Verify a responsive authenticated Baselane session and that no other reconciliation "
                "session is mutating Baselane, then obtain explicit approval before manually running "
                f"`RUN_MONTH={run_month} bash scripts/baselane_monthly_accruals_28th_cron.sh`."
            ),
        )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--run-month", default=date.today().strftime("%Y-%m"))
    parser.add_argument("--current-date", default=date.today().isoformat())
    parser.add_argument("--accrual-report", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    current_date = date.fromisoformat(args.current_date)
    root = args.root.resolve()
    accrual_report = args.accrual_report or root / "reports" / "baselane_monthly_finance_truth_refresh_28th.json"
    report_path = args.report or root / "reports" / "baselane_monthly_accruals_28th_recovery_check.json"
    payload = build_report(run_month=args.run_month, current_date=current_date, accrual_report=accrual_report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
