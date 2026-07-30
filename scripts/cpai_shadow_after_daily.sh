#!/usr/bin/env bash
set -euo pipefail

# Shadow-only companion for the deterministic daily pipeline. It cannot invoke
# any live action and does not change the daily pipeline's exit status.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${WORKSPACE_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
REPORT_DIR="${BASELANE_REPORT_DIR:-$ROOT/reports}"
SUPERVISOR_REPORT="${CPAI_DAILY_SHADOW_REPORT:-$REPORT_DIR/cpai_daily_shadow.json}"

required_reports=(
  "$REPORT_DIR/baselane_daily_run_report.json"
  "$REPORT_DIR/baselane_daily_sync_report.json"
)
for report in "${required_reports[@]}"; do
  if [[ ! -f "$report" ]]; then
    echo "[cpai-shadow] required deterministic report missing: $report" >&2
    exit 75
  fi
done

exec python3 "$ROOT/scripts/cpai_local_supervisor.py" \
  --input-report "${required_reports[0]}" \
  --input-report "${required_reports[1]}" \
  --report "$SUPERVISOR_REPORT"
