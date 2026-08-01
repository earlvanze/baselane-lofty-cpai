#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${WORKSPACE_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
PY="${PYTHON_BIN:-python3}"
APPLY="${BASELANE_NONPROPERTY_CATEGORY_LIVE_APPROVED:-0}"
PREFIX="${BASELANE_NONPROPERTY_CATEGORY_PLAN_PREFIX:-/mnt/c/Users/digit/Dropbox/Projects/cyber-gateway/config/openclaw/workspace/reports/baselane_nonproperty_category_plan}"
APPLY_REPORT="${BASELANE_NONPROPERTY_CATEGORY_APPLY_REPORT:-/mnt/c/Users/digit/Dropbox/Projects/cyber-gateway/config/openclaw/workspace/reports/baselane_nonproperty_category_apply.json}"
AUTH_REPORT="${BASELANE_NONPROPERTY_CATEGORY_AUTH_REPORT:-$ROOT/reports/baselane_auth_recovery_report.json}"
AUTH_TIMEOUT_SECONDS="${BASELANE_NONPROPERTY_CATEGORY_AUTH_TIMEOUT_SECONDS:-120}"
REUSE_FRESH_EXPORT="${BASELANE_NONPROPERTY_CATEGORY_REUSE_FRESH_EXPORT:-0}"

cd "$ROOT"

auth_probe() {
  timeout --kill-after=15s "${AUTH_TIMEOUT_SECONDS}s" \
    "$PY" scripts/baselane_cdp_auth_recovery.py \
      --graphql-auth-smoke \
      --report "$AUTH_REPORT" >/dev/null
}

refresh_sources_and_plan() {
  local force_live_refresh="${1:-0}"
  if [ "$force_live_refresh" = "1" ] || [ "$REUSE_FRESH_EXPORT" != "1" ]; then
    "$PY" scripts/baselane_sync_cdp_deterministic.py >/dev/null
    "$PY" scripts/baselane_export_all_transactions_cdp.py \
      --page-limit 500 \
      --operation-batch-size 1 \
      --report-dir "${BASELANE_REPORT_DIR:-/home/digit/.openclaw/workspace/reports}" \
      >/dev/null
  fi
  "$PY" scripts/baselane_nonproperty_category_plan.py --output-prefix "$PREFIX" >/dev/null
}

# This wrapper runs before the monthly finance-truth auth preflight. Require
# independent live auth here so the fresh export cannot fail first on an
# expired browser cookie.
set +e
auth_probe
auth_rc="$?"
set -e
if [ "$auth_rc" -ne 0 ]; then
  echo "Baselane requires a human-provided authenticated visible browser session; no login recovery is performed." >&2
  exit 3
fi

refresh_sources_and_plan

digest="$("$PY" - "$PREFIX.json" <<'PY'
import json
import sys
from pathlib import Path

sys.path.insert(0, "scripts")
import baselane_nonproperty_category_apply as apply

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(apply.coverage.plan_digest(payload))
PY
)"

if [ "$APPLY" = "1" ]; then
  set +e
  BASELANE_NONPROPERTY_CATEGORY_APPLY=1 "$PY" \
    scripts/baselane_nonproperty_category_apply.py \
    --plan "$PREFIX.json" \
    --report "$APPLY_REPORT" \
    --apply \
    --require-plan-digest "$digest" >/dev/null
  apply_rc="$?"
  set -e
  if [ "$apply_rc" -ne 0 ] && [ "$apply_rc" -ne 2 ]; then
    exit "$apply_rc"
  fi
  applied_count="$("$PY" - "$APPLY_REPORT" <<'PY'
import json
import sys
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(int(report.get("applied_verified_count") or 0))
PY
)"
  if [ "$applied_count" -gt 0 ]; then
    refresh_sources_and_plan 1
  fi
fi

# Always classify the final regenerated plan. This keeps the report paired with
# the current plan even when a pending row settles between export and apply.
set +e
"$PY" scripts/baselane_nonproperty_category_apply.py \
  --plan "$PREFIX.json" \
  --report "$APPLY_REPORT" >/dev/null
verify_rc="$?"
set -e

"$PY" - "$APPLY_REPORT" "$verify_rc" <<'PY'
import json
import sys
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
counts = report.get("status_counts") or {}
allowed = {"already_applied", "applied_verified", "blocked_pending"}
unexpected = {
    name: int(count)
    for name, count in counts.items()
    if name not in allowed and int(count or 0) > 0
}
if unexpected:
    raise SystemExit(
        "Non-Property normalization still has settled actionable/conflicted rows: "
        + json.dumps(unexpected, sort_keys=True)
    )
if int(report.get("failed_count") or 0):
    raise SystemExit("Non-Property normalization has failed live verifications")
PY
