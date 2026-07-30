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

QUEUE_JSON="$ROOT/reports/baselane_source_cleanup_queue.json"
QUEUE_CSV="$ROOT/reports/baselane_source_cleanup_queue.csv"
QUEUE_MD="$ROOT/reports/baselane_source_cleanup_queue.md"
APPLY_REPORT="$ROOT/reports/baselane_source_cleanup_apply_report.json"
PAYLOADS_JSON="$ROOT/reports/baselane_source_cleanup_apply_payloads.json"
AUTH_REPORT="$ROOT/reports/baselane_cdp_auth_recovery_report.json"
DUPLICATE_AUDIT_JSON="$ROOT/reports/baselane_pm_fee_duplicate_lane_audit.json"
DUPLICATE_AUDIT_CSV="$ROOT/reports/baselane_pm_fee_duplicate_lane_audit.csv"
DAILY_REPORT="$ROOT/reports/baselane_daily_sync_report.json"
EOD_PREVIEW="$ROOT/reports/baselane_eod_telegram_preview_report.json"
LEDGER="${BASELANE_LEDGER_PATH:-/home/digit/Dropbox/Projects/assetrail/ECO Systems General Ledger.csv}"
CDP_URL="${BASELANE_CDP_VERSION_URL:-http://127.0.0.1:9222/json/version}"
export AUTH_REPORT

if [ "${BASELANE_SOURCE_CLEANUP_APPLY:-0}" != "1" ]; then
  echo "refusing live Baselane source cleanup: set BASELANE_SOURCE_CLEANUP_APPLY=1" >&2
  exit 2
fi

"$PY" "$ROOT/scripts/baselane_source_cleanup_queue.py" \
  --ledger "$LEDGER" \
  --report "$QUEUE_JSON" \
  --csv "$QUEUE_CSV" \
  --markdown "$QUEUE_MD"

"$PY" "$ROOT/scripts/baselane_apply_source_cleanup_queue.py" \
  --queue "$QUEUE_JSON" \
  --report "$APPLY_REPORT" \
  --payloads "$PAYLOADS_JSON" \
  --timeout-seconds "${BASELANE_SOURCE_CLEANUP_TIMEOUT_SECONDS:-240}" || true

"$PY" "$ROOT/scripts/baselane_cdp_auth_recovery.py" \
  --cdp-url "$CDP_URL" \
  --recover-login \
  --recovery-wait-seconds "${BASELANE_AUTH_RECOVERY_WAIT_SECONDS:-8}" \
  --report "$AUTH_REPORT" || true

AUTH_READY="$(
  "$PY" - <<'PY'
import json
import os
from pathlib import Path
path = Path(os.environ["AUTH_REPORT"])
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    data = {}
print("1" if int(data.get("authenticated_tab_count") or 0) > 0 or int(data.get("verified_authenticated_tab_count") or 0) > 0 else "0")
PY
)"

if [ "$AUTH_READY" != "1" ]; then
  "$PY" "$ROOT/scripts/baselane_daily_sync_report.py" --root "$ROOT" --report "$DAILY_REPORT" || true
  "$PY" "$ROOT/scripts/baselane_eod_telegram_report.py" --dry-run >/dev/null || true
  echo "Baselane auth is not ready; solve visible reCAPTCHA/login, then rerun this script." >&2
  exit 3
fi

"$PY" "$ROOT/scripts/baselane_apply_source_cleanup_queue.py" \
  --queue "$QUEUE_JSON" \
  --report "$APPLY_REPORT" \
  --payloads "$PAYLOADS_JSON" \
  --timeout-seconds "${BASELANE_SOURCE_CLEANUP_TIMEOUT_SECONDS:-240}" \
  --apply || true

BASELANE_CRON_SKIP_WEEKLY=1 "$ROOT/scripts/baselane_cron_run.sh"

"$PY" "$ROOT/scripts/baselane_source_cleanup_queue.py" \
  --ledger "$LEDGER" \
  --report "$QUEUE_JSON" \
  --csv "$QUEUE_CSV" \
  --markdown "$QUEUE_MD" || true

"$PY" "$ROOT/scripts/baselane_pm_fee_duplicate_lane_audit.py" \
  --gl-csv "$LEDGER" \
  --report "$DUPLICATE_AUDIT_JSON" \
  --csv "$DUPLICATE_AUDIT_CSV" || true

"$PY" "$ROOT/scripts/baselane_daily_sync_report.py" --root "$ROOT" --report "$DAILY_REPORT" || true
"$PY" "$ROOT/scripts/baselane_eod_telegram_report.py" --dry-run >/dev/null || true

echo "source cleanup apply/refresh complete"
