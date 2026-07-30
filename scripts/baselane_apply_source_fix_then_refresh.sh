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

if [ "${BASELANE_SOURCE_FIX_APPLY:-0}" != "1" ]; then
  echo "refusing live Baselane source mutation: set BASELANE_SOURCE_FIX_APPLY=1" >&2
  exit 2
fi

"$PY" "$ROOT/scripts/baselane_first_day_pm_fee_source_cleanup_plan.py" \
  --all-months \
  --report "$ROOT/reports/baselane_first_day_pm_fee_source_cleanup_plan.json" \
  --actions-csv "$ROOT/reports/baselane_first_day_pm_fee_source_cleanup_actions.csv" \
  --markdown "$ROOT/reports/baselane_first_day_pm_fee_source_cleanup_plan.md"

FIRST_DAY_PM_FEE_CLEANUP_ACTION_COUNT="$(
  "$PY" - <<'PY'
import json
import os
from pathlib import Path
path = Path(os.environ["ROOT"]) / "reports" / "baselane_first_day_pm_fee_source_cleanup_plan.json"
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    data = {}
print(int(data.get("action_count") or 0))
PY
)"
if [ "$FIRST_DAY_PM_FEE_CLEANUP_ACTION_COUNT" != "0" ]; then
  echo "refusing Baselane source category mutation: $FIRST_DAY_PM_FEE_CLEANUP_ACTION_COUNT first-day AOPS PM-fee source row(s) remain" >&2
  echo "run: BASELANE_FIRST_DAY_PM_FEE_SOURCE_CLEANUP_APPLY=1 bash scripts/baselane_first_day_pm_fee_cleanup_then_refresh.sh" >&2
  exit 2
fi

"$PY" "$ROOT/scripts/split_ledger_public_financials.py" \
  --report "$ROOT/reports/split_ledger_public_financials_last.json"

set +e
"$PY" "$ROOT/scripts/baselane_daily_sync_report.py" \
  --root "$ROOT" \
  --report "$ROOT/reports/baselane_daily_sync_report.json"
daily_sync_before_apply_rc="$?"
set -e
if [ "$daily_sync_before_apply_rc" -ne 0 ]; then
  echo "daily sync report returned rc=$daily_sync_before_apply_rc before source fix apply; continuing because source-fix preflight decides apply safety" >&2
fi

"$PY" "$ROOT/scripts/baselane_ecogl_source_fix_apply.py" --root "$ROOT" --apply

BASELANE_CRON_SKIP_WEEKLY=1 "$ROOT/scripts/baselane_cron_run.sh"

set +e
FORCE_WEEKLY_PASS=1 "$ROOT/scripts/baselane_weekly_file_updates_cron.sh"
weekly_after_apply_rc="$?"
set -e
if [ "$weekly_after_apply_rc" -ne 0 ]; then
  echo "weekly file update cron returned rc=$weekly_after_apply_rc after source fix apply; continuing to refresh readiness reports" >&2
fi

set +e
"$PY" "$ROOT/scripts/baselane_daily_sync_report.py" \
  --root "$ROOT" \
  --report "$ROOT/reports/baselane_daily_sync_report.json"
daily_sync_after_apply_rc="$?"
set -e
if [ "$daily_sync_after_apply_rc" -ne 0 ]; then
  echo "daily sync report returned rc=$daily_sync_after_apply_rc after source fix apply; continuing to refresh readiness reports" >&2
fi

"$PY" "$ROOT/scripts/baselane_monthly_readiness_report.py" \
  --root "$ROOT" \
  --report "$ROOT/reports/baselane_financials_monthly_readiness.json" \
  --markdown "$ROOT/reports/baselane_financials_monthly_readiness.md" || true

"$PY" "$ROOT/scripts/baselane_financials_goal_audit.py" \
  --root "$ROOT" \
  --report "$ROOT/reports/baselane_financials_goal_audit.json" \
  --markdown "$ROOT/reports/baselane_financials_goal_audit.md" || true
