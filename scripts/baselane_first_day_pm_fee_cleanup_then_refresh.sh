#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${WORKSPACE_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
OPENCLAW_ROOT="${OPENCLAW_ROOT:-$(cd "$ROOT/.." && pwd)}"
PY="${PYTHON_BIN:-python3}"
case "$PY" in
  "/usr/bin/env python3"|"env python3") PY="python3" ;;
esac

export ROOT WORKSPACE_ROOT="$ROOT" OPENCLAW_ROOT

if [ "${BASELANE_FIRST_DAY_PM_FEE_SOURCE_CLEANUP_APPLY:-0}" != "1" ]; then
  echo "refusing local ECO GL source cleanup: set BASELANE_FIRST_DAY_PM_FEE_SOURCE_CLEANUP_APPLY=1" >&2
  exit 2
fi

"$PY" "$ROOT/scripts/baselane_first_day_pm_fee_source_cleanup_plan.py" \
  --all-months \
  --apply

"$PY" "$ROOT/scripts/baselane_first_day_pm_fee_audit.py" \
  --all-months

"$PY" "$ROOT/scripts/split_ledger_public_financials.py" \
  --report "$ROOT/reports/split_ledger_public_financials_last.json"

"$PY" "$ROOT/scripts/baselane_daily_sync_report.py" \
  --root "$ROOT" \
  --report "$ROOT/reports/baselane_daily_sync_report.json"

FORCE_WEEKLY_PASS=1 "$ROOT/scripts/baselane_weekly_file_updates_cron.sh"

"$PY" "$ROOT/scripts/baselane_daily_sync_report.py" \
  --root "$ROOT" \
  --report "$ROOT/reports/baselane_daily_sync_report.json"

"$PY" "$ROOT/scripts/baselane_monthly_readiness_report.py" \
  --root "$ROOT" \
  --report "$ROOT/reports/baselane_financials_monthly_readiness.json" \
  --markdown "$ROOT/reports/baselane_financials_monthly_readiness.md" || true

"$PY" "$ROOT/scripts/baselane_financials_goal_audit.py" \
  --root "$ROOT" \
  --report "$ROOT/reports/baselane_financials_goal_audit.json" \
  --markdown "$ROOT/reports/baselane_financials_goal_audit.md" || true
