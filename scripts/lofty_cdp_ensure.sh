#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${WORKSPACE_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
REPORT_DIR="$ROOT/reports"
REPORT_FILE="${LOFTY_CDP_ENSURE_REPORT:-$REPORT_DIR/lofty_cdp_ensure_report.json}"
BASE_URL="${LOFTY_CDP_BASE:-http://127.0.0.1:19222}"
TIMEOUT_SECONDS="${LOFTY_CDP_ENSURE_TIMEOUT_SECONDS:-5}"
POST_LAUNCH_WAIT_SECONDS="${LOFTY_CDP_ENSURE_POST_LAUNCH_WAIT_SECONDS:-5}"
START_SCRIPT="${LOFTY_CDP_START_SCRIPT:-$ROOT/scripts/start_lofty_brave_cdp.ps1}"
POWERSHELL_EXE="${LOFTY_CDP_POWERSHELL_EXE:-}"
if [ -z "$POWERSHELL_EXE" ]; then
  POWERSHELL_EXE="$(command -v powershell.exe 2>/dev/null || true)"
fi
if [ -z "$POWERSHELL_EXE" ] && [ -x /mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe ]; then
  POWERSHELL_EXE=/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe
fi

started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
attempted_launch=false
already_running=false
cdp_available=false
launch_return_code=""
launch_stdout=""
launch_stderr=""
failure_reason=""

probe_cdp() {
  python3 - "$BASE_URL" "$TIMEOUT_SECONDS" <<'PY'
import json
import sys
import urllib.request

base_url = sys.argv[1].rstrip("/")
timeout = int(sys.argv[2])
try:
    with urllib.request.urlopen(base_url + "/json/version", timeout=timeout) as response:
        json.loads(response.read().decode("utf-8", errors="replace"))
    sys.exit(0)
except Exception:
    sys.exit(1)
PY
}

write_report() {
  local status="$1"
  local ended_at
  ended_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  mkdir -p "$REPORT_DIR"
  STATUS="$status" \
  STARTED_AT="$started_at" \
  ENDED_AT="$ended_at" \
  REPORT_FILE="$REPORT_FILE" \
  BASE_URL="$BASE_URL" \
  START_SCRIPT="$START_SCRIPT" \
  ATTEMPTED_LAUNCH="$attempted_launch" \
  ALREADY_RUNNING="$already_running" \
  CDP_AVAILABLE="$cdp_available" \
  LAUNCH_RETURN_CODE="$launch_return_code" \
  LAUNCH_STDOUT="$launch_stdout" \
  LAUNCH_STDERR="$launch_stderr" \
  FAILURE_REASON="$failure_reason" \
  python3 - <<'PY'
import json
import os
from pathlib import Path

def as_bool(value: str) -> bool:
    return value.lower() == "true"

def tail(value: str, limit: int = 2000) -> str:
    return value[-limit:] if value else ""

return_code = os.environ.get("LAUNCH_RETURN_CODE")
report = {
    "job": "lofty-cdp-ensure",
    "status": os.environ["STATUS"],
    "started_at": os.environ["STARTED_AT"],
    "ended_at": os.environ["ENDED_AT"],
    "base_url": os.environ["BASE_URL"],
    "start_script": os.environ["START_SCRIPT"],
    "attempted_launch": as_bool(os.environ["ATTEMPTED_LAUNCH"]),
    "already_running": as_bool(os.environ["ALREADY_RUNNING"]),
    "cdp_available": as_bool(os.environ["CDP_AVAILABLE"]),
    "launch_return_code": int(return_code) if return_code not in (None, "") else None,
    "launch_stdout_tail": tail(os.environ.get("LAUNCH_STDOUT", "")),
    "launch_stderr_tail": tail(os.environ.get("LAUNCH_STDERR", "")),
    "failure_reason": os.environ.get("FAILURE_REASON") or None,
    "report_path": os.environ["REPORT_FILE"],
}
path = Path(os.environ["REPORT_FILE"])
path.parent.mkdir(parents=True, exist_ok=True)
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(path)
print(json.dumps(report, indent=2, sort_keys=True))
PY
}

if probe_cdp; then
  already_running=true
  cdp_available=true
  write_report ok
  exit 0
fi

if [ -z "$POWERSHELL_EXE" ] || [ ! -x "$POWERSHELL_EXE" ]; then
  failure_reason="powershell.exe_not_found"
  write_report failed
  exit 1
fi

if [ ! -f "$START_SCRIPT" ]; then
  failure_reason="start_script_missing"
  write_report failed
  exit 1
fi

attempted_launch=true
start_script_windows="$(wslpath -w "$START_SCRIPT" 2>/dev/null || true)"
if [ -z "$start_script_windows" ]; then
  failure_reason="wslpath_failed"
  write_report failed
  exit 1
fi

stdout_file="$(mktemp)"
stderr_file="$(mktemp)"
"$POWERSHELL_EXE" -NoProfile -ExecutionPolicy Bypass -File "$start_script_windows" -NoElevate >"$stdout_file" 2>"$stderr_file"
launch_return_code="$?"
launch_stdout="$(cat "$stdout_file" 2>/dev/null || true)"
launch_stderr="$(cat "$stderr_file" 2>/dev/null || true)"
rm -f "$stdout_file" "$stderr_file"

sleep "$POST_LAUNCH_WAIT_SECONDS"
if probe_cdp; then
  cdp_available=true
  write_report ok
  exit 0
fi

cdp_available=false
if [ "$launch_return_code" != "0" ]; then
  failure_reason="start_script_failed"
else
  failure_reason="cdp_unavailable_after_launch"
fi
write_report failed
exit 1
