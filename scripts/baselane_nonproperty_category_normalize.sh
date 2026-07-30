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

cd "$ROOT"

auth_probe() {
  timeout --kill-after=15s "${AUTH_TIMEOUT_SECONDS}s" \
    "$PY" scripts/baselane_cdp_auth_recovery.py \
      --graphql-auth-smoke \
      --report "$AUTH_REPORT" >/dev/null
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

"$PY" scripts/baselane_sync_cdp_deterministic.py >/dev/null
"$PY" scripts/baselane_nonproperty_category_plan.py --output-prefix "$PREFIX" >/dev/null

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

needs_final_verify=1
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
    "$PY" scripts/baselane_sync_cdp_deterministic.py >/dev/null
    "$PY" scripts/baselane_nonproperty_category_plan.py --output-prefix "$PREFIX" >/dev/null
  else
    # The apply command already classified every live transaction. With no
    # writes, its report is also the final readback and a second pagination
    # cannot add verification evidence.
    needs_final_verify=0
    verify_rc="$apply_rc"
  fi
fi

if [ "$needs_final_verify" = "1" ]; then
  set +e
  "$PY" scripts/baselane_nonproperty_category_apply.py \
    --plan "$PREFIX.json" \
    --report "$APPLY_REPORT" >/dev/null
  verify_rc="$?"
  set -e
fi

"$PY" - "$APPLY_REPORT" "$verify_rc" <<'PY'
import json
import sys
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
counts = report.get("status_counts") or {}
allowed = {"already_applied", "blocked_pending"}
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
