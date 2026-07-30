#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${WORKSPACE_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
RUN_MONTH="${RUN_MONTH:-$(date +%Y-%m)}"

RUN_MONTH="$RUN_MONTH" \
BASELANE_MONTHLY_ACCRUALS_MONTH="$RUN_MONTH" \
DRY_RUN=0 \
APPLY_BASELANE_MONTHLY_ACCRUALS_LIVE=1 \
RUN_BASELANE_MONTHLY_WEEKLY_REFRESH=1 \
BASELANE_MONTHLY_FINANCE_TRUTH_REFRESH_REPORT="${BASELANE_MONTHLY_FINANCE_TRUTH_REFRESH_REPORT:-$ROOT/reports/baselane_monthly_finance_truth_refresh_28th.json}" \
"$ROOT/scripts/baselane_monthly_finance_truth_refresh.sh"
