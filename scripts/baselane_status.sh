#!/usr/bin/env bash
set -euo pipefail
ROOT="${OPENCLAW_WORKSPACE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
echo "=== Baselane status ==="
echo "time=$(date -Is)"
echo "--- cdp ---"
if curl -fsS http://127.0.0.1:9222/json/version >/dev/null; then
  echo "cdp=ok port=9222"
else
  echo "cdp=down port=9222"
fi
echo "--- session policy ---"
echo "credentials=not-inspected; use a human-authenticated visible browser session"
echo "--- reports ---"
for f in \
  "$ROOT/reports/baselane_statements_download_report.json" \
  "$ROOT/reports/baselane_monthly_run_report.json" \
  "$ROOT/reports/baselane_login_wait_report.json" \
  "$ROOT/reports/baselane_sync_cdp_report.json"; do
  if [ -f "$f" ]; then
    echo "report=$(basename "$f")"
    python3 - <<PY
import json, pathlib
p = pathlib.Path(${f@Q})
d = json.loads(p.read_text())
print(json.dumps({
  'file': p.name,
  'ok': d.get('ok', d.get('status') == 'ok'),
  'checked_at': d.get('checked_at') or d.get('finished_at'),
  'error': d.get('error') or d.get('reason'),
  'new_files_count': d.get('new_files_count'),
  'target_year': d.get('target_year'),
  'target_month': d.get('target_month')
}, indent=2))
PY
  fi
done
echo "--- downloads-dir ---"
find "$ROOT/baselane-statements" -maxdepth 1 -type f -name '*.pdf' | wc -l | awk '{print "pending_download_pdfs=" $1}'
