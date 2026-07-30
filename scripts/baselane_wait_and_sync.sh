#!/usr/bin/env bash
set -euo pipefail

ROOT="${OPENCLAW_WORKSPACE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LOG="$ROOT/logs/baselane_wait_and_sync.log"
MAX_TRIES=${MAX_TRIES:-12}
SLEEP_SEC=${SLEEP_SEC:-300}

check_up() {
  python3 - <<'PY'
import urllib.request
html = urllib.request.urlopen('https://app.baselane.com/login', timeout=15).read().decode('utf-8','ignore')
if 'Temporarily down for maintenance' in html:
    print('DOWN')
else:
    print('UP')
PY
}

for i in $(seq 1 "$MAX_TRIES"); do
  status=$(check_up || echo "DOWN")
  ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  echo "$ts Baselane status: $status (attempt $i/$MAX_TRIES)" | tee -a "$LOG"
  if [ "$status" = "UP" ]; then
    echo "$ts Baselane up. Verifying existing human-authenticated session..." | tee -a "$LOG"
    python3 "$ROOT/scripts/baselane_cdp_auth_recovery.py" --report "$ROOT/reports/baselane_auth_recovery_report.json" | tee -a "$LOG"
    exit $?
  fi
  sleep "$SLEEP_SEC"
done

echo "$(date -u +"%Y-%m-%dT%H:%M:%SZ") Baselane still down after $MAX_TRIES attempts" | tee -a "$LOG"
exit 1
