#!/usr/bin/env bash
set -euo pipefail

ROOT="${WORKSPACE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
NODE="${NODE_BIN:-node}"
PY="${PYTHON_BIN:-python3}"
case "$PY" in
  "/usr/bin/env python3"|"env python3") PY="python3" ;;
esac

export BASELANE_CDP_VERSION_URL="${BASELANE_CDP_VERSION_URL:-http://localhost:9222/json/version}"
export DOWNLOAD_DIR="${DOWNLOAD_DIR:-$HOME/Downloads}"
export BASELANE_STATEMENT_YEAR="${1:-2026}"
export BASELANE_STATEMENT_MONTH="${2:-FEB}"
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

echo "[WORKFLOW] Starting monthly statements for $BASELANE_STATEMENT_MONTH $BASELANE_STATEMENT_YEAR"

# Step 1: Verify the human-provided browser session
echo "[1/3] Verifying existing visible browser session..."
"$PY" "$ROOT/scripts/baselane_cdp_auth_recovery.py" --cdp-url "$BASELANE_CDP_VERSION_URL" --report "$ROOT/reports/baselane_auth_recovery_report.json" >/dev/null

# Step 2: Download statements
echo "[2/3] Downloading statements..."
export BASELANE_MAX_LOAD_MORE=15
export BASELANE_FINAL_WAIT_MS=60000
export BASELANE_CLICK_DELAY_MS=1500
"$NODE" "$ROOT/scripts/baselane_download_statements_cdp.js"

# Step 3: Move downloaded files to canonical locations
echo "[3/3] Organizing downloaded files..."
"$PY" "$ROOT/scripts/baselane_statements_operator.py" \
  --year "$BASELANE_STATEMENT_YEAR" \
  --month "$(echo $BASELANE_STATEMENT_MONTH | tr '[:upper:]' '[:lower:]' | sed 's/jan/1/;s/feb/2/;s/mar/3/;s/apr/4/;s/may/5/;s/jun/6/;s/jul/7/;s/aug/8/;s/sep/9/;s/oct/10/;s/nov/11/;s/dec/12/')" \
  --downloads "$DOWNLOAD_DIR" \
  --real-estate "$REAL_ESTATE_ROOT"

echo "[WORKFLOW] Complete"
