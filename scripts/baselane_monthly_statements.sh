#!/usr/bin/env bash
set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="${WORKSPACE_ROOT:-$(cd "$SCRIPTS_DIR/.." && pwd)}"
DOWNLOAD_DIR="${BASELANE_STATEMENTS_DOWNLOAD_DIR:-$ROOT/baselane-statements}"
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
PERSONAL_ROOT="${BASELANE_PERSONAL_STATEMENTS_ROOT:-$ROOT/pdf-extracts/personal/07 - P&L & Owner Statements/Bank Statements}"
HOLDINGS_ROOT="${BASELANE_HOLDINGS_STATEMENTS_ROOT:-$ROOT/pdf-extracts/business-holdings/07 - P&L & Owner Statements/Bank Statements}"
REPORT="${BASELANE_MONTHLY_STATEMENTS_REPORT:-$ROOT/reports/baselane_monthly_run_report.json}"
LOCKDIR="${BASELANE_MONTHLY_STATEMENTS_LOCKDIR:-$ROOT/tmp/baselane-monthly.lock}"
PY="${PYTHON_BIN:-python3}"
NODE="${NODE_BIN:-node}"
CDP_VERSION_URL="${BASELANE_CDP_VERSION_URL:-http://127.0.0.1:9222/json/version}"
mkdir -p "$ROOT/reports" "$ROOT/tmp" "$ROOT/logs" "$DOWNLOAD_DIR"
cleanup() { rmdir "$LOCKDIR" 2>/dev/null || true; }
trap cleanup EXIT
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  echo "Baselane monthly job already running"
  exit 0
fi

if [ ! -x "$SCRIPTS_DIR/bw_get_baselane.sh" ]; then
  echo "Missing bw_get_baselane.sh"
  exit 1
fi
if ! curl -fsS "$CDP_VERSION_URL" >/dev/null; then
  echo "Brave CDP not reachable at $CDP_VERSION_URL"
  exit 1
fi
if [ -z "${BASELANE_STATEMENTS_DOWNLOAD_DIR:-}" ] && curl -fsS "$CDP_VERSION_URL" | grep -qi 'Windows NT'; then
  WINDOWS_DOWNLOAD_ROOT="/mnt/c/Users/${WINDOWS_USER:-digit}/Downloads"
  if [ -d "$WINDOWS_DOWNLOAD_ROOT" ]; then
    DOWNLOAD_DIR="$WINDOWS_DOWNLOAD_ROOT/baselane-statements"
    mkdir -p "$DOWNLOAD_DIR"
  fi
fi

if [ -n "${BASELANE_STATEMENT_TARGET_YEAR:-}" ] && [ -n "${BASELANE_STATEMENT_TARGET_MONTH:-}" ]; then
  PREV_YEAR="$BASELANE_STATEMENT_TARGET_YEAR"
  PREV_MONTH="$BASELANE_STATEMENT_TARGET_MONTH"
else
  YEAR=$(date +%Y)
  MONTH=$(date +%m)
  if [ "$MONTH" = "01" ]; then
    PREV_MONTH=12
    PREV_YEAR=$((YEAR - 1))
  else
    PREV_MONTH=$((10#$MONTH - 1))
    PREV_YEAR=$YEAR
  fi
fi
case "$PREV_MONTH" in
  1) MONTH_NAME=Jan ;;
  2) MONTH_NAME=Feb ;;
  3) MONTH_NAME=Mar ;;
  4) MONTH_NAME=Apr ;;
  5) MONTH_NAME=May ;;
  6) MONTH_NAME=Jun ;;
  7) MONTH_NAME=Jul ;;
  8) MONTH_NAME=Aug ;;
  9) MONTH_NAME=Sept ;;
  10) MONTH_NAME=Oct ;;
  11) MONTH_NAME=Nov ;;
  12) MONTH_NAME=Dec ;;
  *) echo "Bad month: $PREV_MONTH"; exit 1 ;;
esac

export DOWNLOAD_DIR
export BASELANE_STATEMENT_YEAR="$PREV_YEAR"
export BASELANE_STATEMENT_MONTH="$MONTH_NAME"
export BASELANE_CDP_VERSION_URL="$CDP_VERSION_URL"

echo "=== Baselane Monthly Statements ${PREV_YEAR}-${PREV_MONTH} ==="
"$NODE" "$SCRIPTS_DIR/baselane_download_statements_cdp.js"
"$PY" "$SCRIPTS_DIR/baselane_statements_operator.py" \
  --year "$PREV_YEAR" \
  --month "$PREV_MONTH" \
  --downloads "$DOWNLOAD_DIR" \
  --real-estate "$REAL_ESTATE_ROOT" \
  --personal "$PERSONAL_ROOT" \
  --holdings "$HOLDINGS_ROOT"
"$PY" - <<PY
import json, pathlib, datetime as dt
report = {
  "ok": True,
  "finished_at": dt.datetime.utcnow().isoformat() + "Z",
  "target_year": int("$PREV_YEAR"),
  "target_month": int("$PREV_MONTH"),
  "download_report": str(pathlib.Path("$ROOT/reports/baselane_statements_download_report.json")),
  "status_script": str(pathlib.Path("$ROOT/scripts/baselane_status.sh")),
	  "canonical_real_estate_root": str(pathlib.Path("$REAL_ESTATE_ROOT")),
  "operator_mode": "property+personal+holdings"
}
pathlib.Path("$REPORT").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report, indent=2))
PY
