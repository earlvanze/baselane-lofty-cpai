#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${APP_CHECK:-}" || -z "${BSESSION:-}" ]]; then
  echo "Set APP_CHECK and BSESSION env vars" >&2
  exit 1
fi

OPENCLAW_ROOT="${OPENCLAW_ROOT:-$HOME/.openclaw}"
WORKSPACE_ROOT="${WORKSPACE_ROOT:-$OPENCLAW_ROOT/workspace}"
export WORKSPACE_ROOT

python3 "$WORKSPACE_ROOT/scripts/baselane_export_ledger.py"

python3 - <<'PY'
import json
import os
from pathlib import Path
p = Path(os.environ['WORKSPACE_ROOT']) / 'reports' / 'baselane_export_guard_last.json'
if not p.exists():
    raise SystemExit('missing guard report: ' + str(p))
d = json.loads(p.read_text())
if not d.get('ok'):
    raise SystemExit('guard report marked failed: ' + json.dumps(d.get('violations', [])))
print('guard_report_ok', d.get('output_rows'))
PY

python3 "$WORKSPACE_ROOT/scripts/split_ledger_public_financials.py"
