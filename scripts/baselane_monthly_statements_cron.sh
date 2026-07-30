#!/usr/bin/env bash
set -euo pipefail

export PATH="${HOME}/.local/bin:/home/digit/.npm-global/bin:${HOME}/.npm-global/bin:$PATH"


OPENCLAW_ROOT="${OPENCLAW_ROOT:-$HOME/.openclaw}"
ROOT="${WORKSPACE_ROOT:-$OPENCLAW_ROOT/workspace}"
NODE="${NODE_BIN:-node}"
PY="${PYTHON_BIN:-python3}"
case "$PY" in
  "/usr/bin/env python3"|"env python3") PY="python3" ;;
esac

REAL_ESTATE_ROOT="${REAL_ESTATE_ROOT:-}"
if [ -z "$REAL_ESTATE_ROOT" ]; then
  for candidate in \
    "/mnt/c/Users/digit/Dropbox/Real Estate" \
    "/mnt/c/users/digit/Dropbox/Real Estate" \
    "/data/Dropbox/Real Estate" \
    "$HOME/Dropbox/Real Estate" \
    "/home/digit/Dropbox/Real Estate" \
    ; do
    if [ -d "$candidate" ]; then
      REAL_ESTATE_ROOT="$candidate"
      break
    fi
  done
fi
REAL_ESTATE_ROOT="${REAL_ESTATE_ROOT:-/mnt/c/Users/digit/Dropbox/Real Estate}"

# Require a human-provided, already authenticated Baselane Statements session.
export BASELANE_CDP_VERSION_URL="${BASELANE_CDP_VERSION_URL:-http://localhost:9222/json/version}"
AUTH_REPORT="${BASELANE_AUTH_RECOVERY_REPORT:-$ROOT/reports/baselane_auth_recovery_report.json}"
"$PY" "$ROOT/scripts/baselane_cdp_auth_recovery.py" --cdp-url "$BASELANE_CDP_VERSION_URL" --report "$AUTH_REPORT" >/dev/null

# After any UI-driven statement downloads during the wait window, sweep Downloads into canonical folders.
read -r YEAR MONTH <<EOF
$($PY - <<'PY'
import datetime as dt
now = dt.date.today().replace(day=1)
prev = (now - dt.timedelta(days=1)).replace(day=1)
print(prev.year, prev.month)
PY
)
EOF

"$PY" "$ROOT/scripts/baselane_statements_operator.py" \
  --year "$YEAR" \
  --month "$MONTH" \
  --real-estate "$REAL_ESTATE_ROOT"
