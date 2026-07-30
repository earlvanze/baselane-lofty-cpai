#!/usr/bin/env bash
set -euo pipefail

# baselane_monthly_statements_v2.sh
#
# Anti-bot-detection monthly statement download workflow:
# 1. Check if Brave has authenticated Baselane session (no auto-login)
# 2. If authenticated, proceed with deterministic statement download
# 3. If not authenticated, report and exit (require manual login)
# 4. Clean up tabs after completion
#
# Cron schedule: 1st of each month at 10:00 AM
# Env: BASELANE_CDP_VERSION_URL defaults to local Brave

OPENCLAW_ROOT="${OPENCLAW_ROOT:-$HOME/.openclaw}"
ROOT="${WORKSPACE_ROOT:-$OPENCLAW_ROOT/workspace}"
NODE="${NODE_BIN:-node}"
PY="${PYTHON_BIN:-python3}"
case "$PY" in
  "/usr/bin/env python3"|"env python3") PY="python3" ;;
esac
REPORTS="$ROOT/reports"
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

mkdir -p "$REPORTS"

# Use local Brave CDP port (non-headless)
export BASELANE_CDP_VERSION_URL="${BASELANE_CDP_VERSION_URL:-http://localhost:9222/json/version}"
export BASELANE_TARGET_URL="https://app.baselane.com/banking/statements"
export BASELANE_WAIT_MS="${BASELANE_WAIT_MS:-60000}"  # 1 minute for auth check

echo "[$(date)] Starting Baselane monthly statement download..."

# Step 1: Check authentication status (no login attempt)
echo "[$(date)] Checking Baselane authentication..."
if ! "$PY" "$ROOT/scripts/baselane_cdp_auth_recovery.py" --cdp-url "$BASELANE_CDP_VERSION_URL" --report "$REPORTS/baselane_auth_recovery_report.json"; then
  echo "[$(date)] ERROR: Baselane not authenticated. Manual login required."
  echo "[$(date)] Please log into Baselane in Brave browser, then retry."
  exit 1
fi

# Step 2: Download statements
echo "[$(date)] Authentication confirmed. Downloading statements..."
"$NODE" "$ROOT/scripts/baselane_statements_deterministic.js"

# Step 3: Organize downloaded files
echo "[$(date)] Organizing downloaded statements..."
read -r YEAR MONTH <<EOF
$($PY - <<'PY'
import datetime as dt
now = dt.date.today().replace(day=1)
prev = (now - dt.timedelta(days=1)).replace(day=1)
print(prev.year, prev.month)
PY
)
EOF

if [ -f "$ROOT/scripts/baselane_statements_operator.py" ]; then
  "$PY" "$ROOT/scripts/baselane_statements_operator.py" \
    --year "$YEAR" \
    --month "$MONTH" \
    --real-estate "$REAL_ESTATE_ROOT" || true
fi

echo "[$(date)] Monthly statement download complete."
