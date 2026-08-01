#!/usr/bin/env bash
set -euo pipefail

export PATH="${HOME}/.local/bin:/home/digit/.npm-global/bin:${HOME}/.npm-global/bin:$PATH"

if ! mountpoint -q /mnt/c || [ ! -d /mnt/c/Users/digit/Dropbox ]; then
  echo "[baselane-daily] Dropbox mount unavailable at /mnt/c/Users/digit/Dropbox" >&2
  exit 75
fi

cdp_reachable() {
  command -v curl >/dev/null 2>&1 && timeout 4 curl -fsS -H 'Host: localhost' "$1" >/dev/null 2>&1
}

cdp_json() {
  timeout 4 curl -fsS -H 'Host: localhost' "$1"
}

cdp_has_baselane_tab() {
  local version_url="$1"
  local base_url="${version_url%/json/version}"
  command -v curl >/dev/null 2>&1 || return 1
  cdp_json "$base_url/json/list" 2>/dev/null | python3 -c 'import json, sys
try:
    tabs = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
if not isinstance(tabs, list):
    raise SystemExit(1)
for tab in tabs:
    if not isinstance(tab, dict):
        continue
    if str(tab.get("type") or "page").lower() != "page":
        continue
    url = str(tab.get("url") or "").lower()
    if url.startswith("https://app.baselane.com/") or url.startswith("http://app.baselane.com/"):
        raise SystemExit(0)
raise SystemExit(1)'
}

cdp_has_authenticated_baselane_tab() {
  local version_url="$1"
  local base_url="${version_url%/json/version}"
  command -v curl >/dev/null 2>&1 || return 1
  cdp_json "$base_url/json/list" 2>/dev/null | python3 -c 'import json, sys
try:
    tabs = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
if not isinstance(tabs, list):
    raise SystemExit(1)
for tab in tabs:
    if not isinstance(tab, dict):
        continue
    if str(tab.get("type") or "page").lower() != "page":
        continue
    url = str(tab.get("url") or "").lower()
    if not (url.startswith("https://app.baselane.com/") or url.startswith("http://app.baselane.com/")):
        continue
    if "/login" in url or "/session-expired" in url or "/error" in url:
        continue
    raise SystemExit(0)
raise SystemExit(1)'
}

set_baselane_cdp_endpoint() {
  local version_url="$1"
  local base_url="${version_url%/json/version}"
  local endpoint="${base_url#http://}"
  endpoint="${endpoint#https://}"
  endpoint="${endpoint%%/*}"
  local endpoint_host endpoint_port
  if [[ "$endpoint" == \[*\]:* ]]; then
    endpoint_host="${endpoint%:*}"
    endpoint_host="${endpoint_host#[}"
    endpoint_host="${endpoint_host%]}"
    endpoint_port="${endpoint##]:}"
  else
    endpoint_host="${endpoint%%:*}"
    endpoint_port="${endpoint##*:}"
  fi
  export BASELANE_CDP_URL="$base_url"
  export BASELANE_CDP_HOST="$endpoint_host"
  export BASELANE_CDP_PORT="$endpoint_port"
  export BASELANE_CDP_VERSION_URL="$version_url"
}

configure_baselane_cdp() {
  if [ -n "${BASELANE_CDP_VERSION_URL:-}" ]; then
    if cdp_reachable "$BASELANE_CDP_VERSION_URL" && cdp_has_authenticated_baselane_tab "$BASELANE_CDP_VERSION_URL"; then
      set_baselane_cdp_endpoint "$BASELANE_CDP_VERSION_URL"
      return 0
    fi
  fi
  if [ -n "${BASELANE_CDP_URL:-}" ]; then
    local env_cdp_url="${BASELANE_CDP_URL%/}"
    if [[ "$env_cdp_url" == */json/version ]]; then
      if cdp_reachable "$env_cdp_url" && cdp_has_authenticated_baselane_tab "$env_cdp_url"; then
        set_baselane_cdp_endpoint "$env_cdp_url"
        return 0
      fi
    else
      if cdp_reachable "$env_cdp_url/json/version" && cdp_has_authenticated_baselane_tab "$env_cdp_url/json/version"; then
        set_baselane_cdp_endpoint "$env_cdp_url/json/version"
        return 0
      fi
    fi
  fi

  local cyber_ts_ip="${BASELANE_CDP_TAILNET_IP:-}"
  if [ -z "$cyber_ts_ip" ]; then
    cyber_ts_ip="$(tailscale ip --4 cyber 2>/dev/null || true)"
  fi
  if [ -z "$cyber_ts_ip" ]; then
    cyber_ts_ip="100.115.208.70"
  fi

  local candidates=(
    "http://host.docker.internal:19222/json/version"
    "http://127.0.0.1:19222/json/version"
    "http://${cyber_ts_ip}:19222/json/version"
  )
  local candidate
  for candidate in "${candidates[@]}"; do
    if cdp_reachable "$candidate" && cdp_has_authenticated_baselane_tab "$candidate"; then
      set_baselane_cdp_endpoint "$candidate"
      return 0
    fi
  done
  for candidate in "${candidates[@]}"; do
    if cdp_reachable "$candidate" && cdp_has_baselane_tab "$candidate"; then
      set_baselane_cdp_endpoint "$candidate"
      return 0
    fi
  done
  for candidate in "${candidates[@]}"; do
    if cdp_reachable "$candidate"; then
      set_baselane_cdp_endpoint "$candidate"
      return 0
    fi
  done
  if [ -n "${BASELANE_CDP_VERSION_URL:-}" ] && cdp_reachable "$BASELANE_CDP_VERSION_URL"; then
    set_baselane_cdp_endpoint "$BASELANE_CDP_VERSION_URL"
    return 0
  fi
  if [ -n "${BASELANE_CDP_URL:-}" ]; then
    local fallback_cdp_url="${BASELANE_CDP_URL%/}"
    [[ "$fallback_cdp_url" == */json/version ]] || fallback_cdp_url="$fallback_cdp_url/json/version"
    if cdp_reachable "$fallback_cdp_url"; then
      set_baselane_cdp_endpoint "$fallback_cdp_url"
      return 0
    fi
  fi
  set_baselane_cdp_endpoint "http://127.0.0.1:19222/json/version"
}

configure_baselane_cdp

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${WORKSPACE_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
OPENCLAW_ROOT="${OPENCLAW_ROOT:-$(cd "$ROOT/.." && pwd)}"
export OPENCLAW_ROOT ROOT WORKSPACE_ROOT="$ROOT"
NODE="${NODE_BIN:-node}"
PY="${PYTHON_BIN:-python3}"
case "$PY" in
  "/usr/bin/env python3"|"env python3") PY="python3" ;;
esac
WORKBOOK_PY="${BASELANE_WORKBOOK_PYTHON:-$PY}"
if ! "$WORKBOOK_PY" -c 'import openpyxl' >/dev/null 2>&1; then
  if [ -x /home/linuxbrew/.linuxbrew/bin/python3 ] && /home/linuxbrew/.linuxbrew/bin/python3 -c 'import openpyxl' >/dev/null 2>&1; then
    WORKBOOK_PY="/home/linuxbrew/.linuxbrew/bin/python3"
  fi
fi
# Native split mutations require an explicit scheduler/environment opt-in.
export BASELANE_NATIVE_SPLIT_APPLY="${BASELANE_NATIVE_SPLIT_APPLY:-0}"

if [ -z "${DROPBOX_ROOT:-}" ]; then
  if [ -d /mnt/c/Users/digit/Dropbox ]; then
    DROPBOX_ROOT="/mnt/c/Users/digit/Dropbox"
  elif [ -d /data/Dropbox ]; then
    DROPBOX_ROOT="/data/Dropbox"
  elif [ -d "$HOME/Dropbox" ]; then
    DROPBOX_ROOT="$HOME/Dropbox"
  elif [ -d /home/digit/Dropbox ]; then
    DROPBOX_ROOT="/home/digit/Dropbox"
  else
    DROPBOX_ROOT="/mnt/c/Users/digit/Dropbox"
  fi
fi

if [ -z "${BASELANE_LEDGER_DIR:-}" ]; then
  if [ -d "$DROPBOX_ROOT/Projects/assetrail" ]; then
    BASELANE_LEDGER_DIR="$DROPBOX_ROOT/Projects/assetrail"
  elif [ -d "$DROPBOX_ROOT/Projects/transaction_tracker" ]; then
    BASELANE_LEDGER_DIR="$DROPBOX_ROOT/Projects/transaction_tracker"
  else
    BASELANE_LEDGER_DIR="$DROPBOX_ROOT/Projects/assetrail"
  fi
fi

export DROPBOX_ROOT BASELANE_LEDGER_DIR
BASELANE_LEDGER_PATH="$BASELANE_LEDGER_DIR/ECO Systems General Ledger.csv"
export BASELANE_LEDGER_PATH
REPORT_DIR="$ROOT/reports"
SCOPE_GUARD_FILE="$REPORT_DIR/baselane_daily_scope_guard.json"
RUN_REPORT_FILE="$REPORT_DIR/baselane_daily_run_report.json"
SYNC_REPORT_FILE="$REPORT_DIR/baselane_daily_sync_report.json"
DISK_PREFLIGHT_REPORT_FILE="$REPORT_DIR/baselane_daily_disk_space_preflight_report.json"
BASELANE_DISK_PREFLIGHT_MIN_FREE_MIB="${BASELANE_DISK_PREFLIGHT_MIN_FREE_MIB:-10240}"
BASELANE_DISK_PREFLIGHT_PATH_TIMEOUT_SECONDS="${BASELANE_DISK_PREFLIGHT_PATH_TIMEOUT_SECONDS:-15}"
BASELANE_DISK_PREFLIGHT_TIMEOUT_SECONDS="${BASELANE_DISK_PREFLIGHT_TIMEOUT_SECONDS:-90}"
BASELANE_DISK_PREFLIGHT_KILL_AFTER_SECONDS="${BASELANE_DISK_PREFLIGHT_KILL_AFTER_SECONDS:-15}"
BASELANE_DISK_PREFLIGHT_RETRY_DELAY_SECONDS="${BASELANE_DISK_PREFLIGHT_RETRY_DELAY_SECONDS:-10}"
export BASELANE_DISK_PREFLIGHT_MIN_FREE_MIB BASELANE_DISK_PREFLIGHT_TIMEOUT_SECONDS BASELANE_DISK_PREFLIGHT_KILL_AFTER_SECONDS BASELANE_DISK_PREFLIGHT_RETRY_DELAY_SECONDS
STALE_FINANCIAL_ARTIFACT_GUARD_FILE="${STALE_FINANCIAL_ARTIFACT_GUARD_FILE:-$REPORT_DIR/baselane_stale_financial_artifact_guard.json}"
STALE_FINANCIAL_ARTIFACT_GUARD_TIMEOUT_SECONDS="${BASELANE_STALE_FINANCIAL_ARTIFACT_GUARD_TIMEOUT_SECONDS:-60}"
RUN_HISTORY_FILE="$REPORT_DIR/baselane_daily_run_history.jsonl"
RUN_STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
RUN_STARTED_EPOCH="$(date +%s)"
# Soft self-timeout: checkpoint and exit before the cron's hard kill.
# Default 3300s (55min) gives a 5min buffer before the 3600s cron timeout.
BASELANE_SOFT_TIMEOUT_SECONDS="${BASELANE_SOFT_TIMEOUT_SECONDS:-3300}"
soft_timeout_check() {
  local now_epoch
  now_epoch="$(date +%s)"
  if [ $((now_epoch - RUN_STARTED_EPOCH)) -ge "$BASELANE_SOFT_TIMEOUT_SECONDS" ]; then
    echo "[baselane] soft self-timeout reached after ${BASELANE_SOFT_TIMEOUT_SECONDS}s at step=$CURRENT_STEP; checkpointing" >&2
    INTERRUPT_SIGNAL="SOFT_TIMEOUT"
    exit 142
  fi
}
CURRENT_STEP="init"
INTERRUPT_SIGNAL=""
SESSION_SEED_STATUS="not_started"
AUTH_PREFLIGHT_STATUS="not_started"
SYNC_STATUS="not_started"
SYNC_ORIGINAL_STATUS=""
SYNC_RECOVERY_STATUS="not_required"
SYNC_RECOVERED_BY=""
SYNC_RECOVERY_REPORT=""
HUMAN_PACED_FALLBACK_STATUS="not_started"
UTILITY_STATUS="not_started"
TOKENOMICS_STATUS="not_started"
NO_DAO_MORTGAGE_CASH_BASIS_STATUS="not_started"
HEMLANE_LIVE_STATUS="not_started"
HEMLANE_AUTO_TAG_STATUS="not_started"
HEMLANE_AUTO_TAG_APPLIED_COUNT="0"
HEMLANE_AUTO_TAG_REEXPORT_STATUS="not_required"
WEEKLY_STATUS="skipped"
MONTHLY_STATEMENTS_STATUS="skipped"
LOCAL_MODEL_PREFLIGHT_STATUS="not_started"
SOURCE_CASH_STATUS="not_started"
YHOME_OPERATING_CASH_STATUS="not_started"
DISK_PREFLIGHT_STATUS="not_started"
DISK_PREFLIGHT_ATTEMPTS="0"
STALE_FINANCIAL_ARTIFACT_GUARD_STATUS="not_started"
GIT_PUSH_STATUS="not_started"
SOURCE_CASH_TIMEOUT_SECONDS="${BASELANE_SOURCE_CASH_AUDIT_TIMEOUT_SECONDS:-900}"
SOURCE_CASH_APPLY="${BASELANE_SOURCE_CASH_BALANCE_APPLY:-0}"
ASSETRAIL_PUSH_ENABLED="${BASELANE_ASSETRAIL_PUSH_ENABLED:-0}"

write_source_cash_timeout_report() {
  local reason="$1"
  mkdir -p "$REPORT_DIR"
  SOURCE_CASH_REPORT_FILE="$ROOT/reports/baselane_daily_source_cash_balance_report.json" \
  SOURCE_CASH_TIMEOUT_SECONDS="$SOURCE_CASH_TIMEOUT_SECONDS" \
  SOURCE_CASH_TIMEOUT_REASON="$reason" \
  SOURCE_CASH_APPLY="$SOURCE_CASH_APPLY" \
  "$PY" - <<'PY' || true
import json
import os
from datetime import datetime, timezone
from pathlib import Path

path = Path(os.environ["SOURCE_CASH_REPORT_FILE"])
report = {
    "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "job": "baselane-daily-source-cash-balance",
    "status": "review",
    "mode": "apply" if os.environ["SOURCE_CASH_APPLY"] == "1" else "dry_run",
    "effective_mode": "unknown",
    "apply_requested": os.environ["SOURCE_CASH_APPLY"] == "1",
    "reason": os.environ["SOURCE_CASH_TIMEOUT_REASON"],
    "timeout_seconds": int(os.environ["SOURCE_CASH_TIMEOUT_SECONDS"]),
    "checked_workbook_count": 0,
    "checked_property_count": 0,
    "split_scope_expected_property_count": 0,
    "split_scope_missing_property_count": 0,
    "update_count": 0,
    "violation_count": 0,
    "missing_row_count": 0,
    "missing_month_column_count": 0,
    "unreadable_count": 0,
    "report": str(path),
}
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(path)
progress = path.with_suffix(path.suffix + ".progress")
try:
    progress.unlink()
except FileNotFoundError:
    pass
PY
}

run_source_cash_balance_audit() {
  SOURCE_CASH_STATUS="in_progress"
  if [ ! -f "$ROOT/scripts/baselane_daily_source_cash_balance_audit.py" ]; then
    SOURCE_CASH_STATUS="skipped_missing_script"
    return 0
  fi
  set +e
  local source_cash_command=(
    "$WORKBOOK_PY" "$ROOT/scripts/baselane_daily_source_cash_balance_audit.py"
    --gl-csv "$BASELANE_LEDGER_PATH"
    --real-estate-root "$DROPBOX_ROOT/Real Estate"
    --split-report "$ROOT/reports/split_ledger_public_financials_last.json"
    --report "$ROOT/reports/baselane_daily_source_cash_balance_report.json"
  )
  if [ "$SOURCE_CASH_APPLY" = "1" ]; then
    source_cash_command+=(--apply)
  fi
  timeout --kill-after=30s "${SOURCE_CASH_TIMEOUT_SECONDS}s" "${source_cash_command[@]}" >/dev/null
  local source_cash_rc="$?"
  set -e
  if [ "$source_cash_rc" -eq 0 ]; then
    SOURCE_CASH_STATUS="ok"
  elif [ "$source_cash_rc" -eq 124 ] || [ "$source_cash_rc" -eq 137 ]; then
    SOURCE_CASH_STATUS="review_timeout_nonfatal_${SOURCE_CASH_TIMEOUT_SECONDS}s"
    if [ "$source_cash_rc" -eq 137 ]; then
      write_source_cash_timeout_report "source_cash_audit_killed_after_timeout"
      echo "[baselane] daily source-cash balance audit was killed after the ${SOURCE_CASH_TIMEOUT_SECONDS}s timeout; continuing fail-closed" >&2
    else
      write_source_cash_timeout_report "source_cash_audit_timeout"
      echo "[baselane] daily source-cash balance audit timed out after ${SOURCE_CASH_TIMEOUT_SECONDS}s; continuing fail-closed" >&2
    fi
  else
    SOURCE_CASH_STATUS="review_nonfatal_rc_${source_cash_rc}"
    echo "[baselane] daily source-cash balance audit returned rc=${source_cash_rc}; daily report will surface the issue" >&2
  fi
  return 0
}

run_daily_yhome_operating_cash_sync() {
  YHOME_OPERATING_CASH_STATUS="in_progress"
  local script="$ROOT/scripts/baselane_daily_yhome_operating_cash_sync.py"
  if [ ! -f "$script" ]; then
    YHOME_OPERATING_CASH_STATUS="skipped_missing_script"
    return 0
  fi
  if [ "$SYNC_STATUS" != "ok" ] || [ "$SOURCE_CASH_STATUS" != "ok" ]; then
    YHOME_OPERATING_CASH_STATUS="skipped_upstream_not_clean"
    return 0
  fi
  set +e
  timeout --kill-after=30s "${YHOME_DAILY_SYNC_TIMEOUT_SECONDS:-300}s" "$PY" "$script" >/dev/null
  local rc="$?"
  set -e
  if [ "$rc" -eq 0 ]; then
    YHOME_OPERATING_CASH_STATUS="ok"
  elif [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
    YHOME_OPERATING_CASH_STATUS="review_timeout"
  else
    YHOME_OPERATING_CASH_STATUS="review_nonfatal_rc_${rc}"
  fi
  return 0
}

write_assetrail_push_report() {
  local status="$1"
  local reason="${2:-}"
  local git_head="${3:-}"
  local git_commit_timestamp="${4:-}"
  local ledger_git_status="${5:-}"
  mkdir -p "$REPORT_DIR"
  REPORT_DIR="$REPORT_DIR" \
  BASELANE_LEDGER_DIR="$BASELANE_LEDGER_DIR" \
  BASELANE_LEDGER_PATH="$BASELANE_LEDGER_PATH" \
  BASELANE_ASSETRAIL_PUSH_STATUS="$status" \
  BASELANE_ASSETRAIL_PUSH_REASON="$reason" \
  BASELANE_ASSETRAIL_GIT_HEAD="$git_head" \
  BASELANE_ASSETRAIL_GIT_COMMIT_TIMESTAMP="$git_commit_timestamp" \
  BASELANE_ASSETRAIL_LEDGER_GIT_STATUS="$ledger_git_status" \
  "$PY" - <<'PY'
import json
import os
from datetime import datetime, timezone
from pathlib import Path

report_dir = Path(os.environ["REPORT_DIR"])
report = {
    "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "status": os.environ.get("BASELANE_ASSETRAIL_PUSH_STATUS") or "unknown",
    "reason": os.environ.get("BASELANE_ASSETRAIL_PUSH_REASON") or None,
    "ledger_dir": os.environ.get("BASELANE_LEDGER_DIR"),
    "ledger_path": os.environ.get("BASELANE_LEDGER_PATH"),
    "git_head": os.environ.get("BASELANE_ASSETRAIL_GIT_HEAD") or None,
    "git_commit_timestamp": os.environ.get("BASELANE_ASSETRAIL_GIT_COMMIT_TIMESTAMP") or None,
    "ledger_git_status": os.environ.get("BASELANE_ASSETRAIL_LEDGER_GIT_STATUS") or None,
}
path = report_dir / "baselane_assetrail_push_report.json"
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(path)
PY
}

push_baselane_ledger_repo() {
  if [ "${BASELANE_GIT_PUSH_ENABLED:-1}" != "1" ]; then
    GIT_PUSH_STATUS="skipped_by_env"
    write_assetrail_push_report "$GIT_PUSH_STATUS" "disabled_by_env"
    return 0
  fi
  if ! command -v git >/dev/null 2>&1; then
    GIT_PUSH_STATUS="skipped_missing_git"
    write_assetrail_push_report "$GIT_PUSH_STATUS" "missing_git"
    echo "[baselane] Skipping assetrail GitHub push: git is not installed" >&2
    return 0
  fi
  local git_cmd
  git_cmd=(git -c "safe.directory=$BASELANE_LEDGER_DIR" -C "$BASELANE_LEDGER_DIR")
  if ! "${git_cmd[@]}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    GIT_PUSH_STATUS="skipped_not_git_repo"
    write_assetrail_push_report "$GIT_PUSH_STATUS" "not_git_repo"
    echo "[baselane] Skipping assetrail GitHub push: $BASELANE_LEDGER_DIR is not a git repo" >&2
    return 0
  fi

  local index_lock
  index_lock="$BASELANE_LEDGER_DIR/.git/index.lock"
  if [ -e "$index_lock" ]; then
    if command -v lsof >/dev/null 2>&1 && lsof "$index_lock" >/dev/null 2>&1; then
      GIT_PUSH_STATUS="skipped_active_git_index_lock"
      write_assetrail_push_report "$GIT_PUSH_STATUS" "active_git_index_lock"
      echo "[baselane] Skipping assetrail GitHub push: active git index lock at $index_lock" >&2
      return 0
    fi
    local stale_lock_seconds
    local lock_mtime
    local lock_age
    local stale_lock_path
    stale_lock_seconds="${BASELANE_ASSETRAIL_STALE_LOCK_SECONDS:-21600}"
    lock_mtime="$(stat -c %Y "$index_lock" 2>/dev/null || echo 0)"
    lock_age=$(( $(date +%s) - lock_mtime ))
    if [ "$lock_age" -lt "$stale_lock_seconds" ]; then
      GIT_PUSH_STATUS="skipped_recent_git_index_lock"
      write_assetrail_push_report "$GIT_PUSH_STATUS" "recent_git_index_lock"
      echo "[baselane] Skipping assetrail GitHub push: recent git index lock at $index_lock age=${lock_age}s" >&2
      return 0
    fi
    stale_lock_path="$index_lock.stale-$(date -u +%Y%m%dT%H%M%SZ)"
    if mv "$index_lock" "$stale_lock_path"; then
      echo "[baselane] Moved stale assetrail git index lock aside: $stale_lock_path" >&2
    else
      GIT_PUSH_STATUS="failed_stale_git_index_lock"
      write_assetrail_push_report "$GIT_PUSH_STATUS" "stale_git_index_lock_move_failed"
      echo "[baselane] Cannot move stale assetrail git index lock at $index_lock" >&2
      return 9
    fi
  fi

  local ledger_file
  ledger_file="$(basename "$BASELANE_LEDGER_PATH")"
  if [ ! -f "$BASELANE_LEDGER_DIR/$ledger_file" ]; then
    GIT_PUSH_STATUS="failed_missing_ledger"
    write_assetrail_push_report "$GIT_PUSH_STATUS" "missing_ledger"
    echo "[baselane] Cannot push assetrail repo: missing $BASELANE_LEDGER_DIR/$ledger_file" >&2
    return 8
  fi

  "${git_cmd[@]}" add -- "$ledger_file"
  if "${git_cmd[@]}" diff --cached --quiet -- "$ledger_file"; then
    GIT_PUSH_STATUS="no_ledger_changes"
  else
    if ! "${git_cmd[@]}" commit -m "chore: update General Ledger from Baselane export [$(date -u +%Y%m%d-%H%M%S)]"; then
      GIT_PUSH_STATUS="failed_commit"
      write_assetrail_push_report "$GIT_PUSH_STATUS" "git_commit_failed"
      return 9
    fi
    GIT_PUSH_STATUS="committed"
  fi
  if ! "${git_cmd[@]}" push; then
    GIT_PUSH_STATUS="failed_push"
    write_assetrail_push_report "$GIT_PUSH_STATUS" "git_push_failed"
    return 10
  fi
  if [ "$GIT_PUSH_STATUS" = "committed" ]; then
    GIT_PUSH_STATUS="committed_and_pushed"
  else
    GIT_PUSH_STATUS="pushed_no_ledger_changes"
  fi
  local git_head
  local git_commit_timestamp
  local ledger_git_status
  git_head="$("${git_cmd[@]}" rev-parse --short HEAD 2>/dev/null || true)"
  git_commit_timestamp="$("${git_cmd[@]}" log -1 --format=%cI -- "$ledger_file" 2>/dev/null || true)"
  ledger_git_status="$("${git_cmd[@]}" status --short -- "$ledger_file" 2>/dev/null || true)"
  write_assetrail_push_report "$GIT_PUSH_STATUS" "ok" "$git_head" "$git_commit_timestamp" "$ledger_git_status"
}

run_pre_push_source_guards() {
  local cleanup_rc=0
  local audit_rc=0
  if [ -f "$ROOT/scripts/baselane_first_day_pm_fee_source_cleanup_plan.py" ]; then
    set +e
    BASELANE_FIRST_DAY_PM_FEE_SOURCE_CLEANUP_APPLY=1 \
      "$PY" "$ROOT/scripts/baselane_first_day_pm_fee_source_cleanup_plan.py" \
      --gl-csv "$BASELANE_LEDGER_PATH" \
      --all-months \
      --apply \
      --report "$ROOT/reports/baselane_first_day_pm_fee_source_cleanup_plan.json" \
      --actions-csv "$ROOT/reports/baselane_first_day_pm_fee_source_cleanup_actions.csv" \
      --markdown "$ROOT/reports/baselane_first_day_pm_fee_source_cleanup_plan.md" >/dev/null
    cleanup_rc="$?"
    set -e
    if [ "$cleanup_rc" -ne 0 ]; then
      echo "[baselane] Blocking AssetRail push: first-day PM fee source cleanup failed rc=$cleanup_rc" >&2
      return "$cleanup_rc"
    fi
  fi
  if [ -f "$ROOT/scripts/baselane_first_day_pm_fee_audit.py" ]; then
    set +e
    "$PY" "$ROOT/scripts/baselane_first_day_pm_fee_audit.py" \
      --gl-csv "$BASELANE_LEDGER_PATH" \
      --all-months \
      --report "$ROOT/reports/baselane_first_day_pm_fee_audit.json" >/dev/null
    audit_rc="$?"
    set -e
    if [ "$audit_rc" -ne 0 ]; then
      echo "[baselane] Blocking AssetRail push: first-day PM fee audit failed rc=$audit_rc" >&2
      return "$audit_rc"
    fi
  fi
  return 0
}

write_run_report() {
  local rc="$1"
  local status="$2"
  local failed_step="${3:-}"
  mkdir -p "$REPORT_DIR"
  BASELANE_RUN_RC="$rc" \
  BASELANE_RUN_STATUS="$status" \
  BASELANE_FAILED_STEP="$failed_step" \
  BASELANE_RUN_STARTED_AT="$RUN_STARTED_AT" \
  BASELANE_RUN_STARTED_EPOCH="$RUN_STARTED_EPOCH" \
  BASELANE_RUN_ENDED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  BASELANE_RUN_ENDED_EPOCH="$(date +%s)" \
  BASELANE_RUN_REPORT_FILE="$RUN_REPORT_FILE" \
  BASELANE_RUN_HISTORY_FILE="$RUN_HISTORY_FILE" \
  BASELANE_LEDGER_PATH="$BASELANE_LEDGER_PATH" \
  BASELANE_SESSION_SEED_STATUS="$SESSION_SEED_STATUS" \
  BASELANE_AUTH_PREFLIGHT_STATUS="$AUTH_PREFLIGHT_STATUS" \
  BASELANE_SYNC_STATUS="$SYNC_STATUS" \
  BASELANE_SYNC_ORIGINAL_STATUS="$SYNC_ORIGINAL_STATUS" \
  BASELANE_SYNC_RECOVERY_STATUS="$SYNC_RECOVERY_STATUS" \
  BASELANE_SYNC_RECOVERED_BY="$SYNC_RECOVERED_BY" \
  BASELANE_SYNC_RECOVERY_REPORT="$SYNC_RECOVERY_REPORT" \
  BASELANE_HUMAN_PACED_FALLBACK_STATUS="$HUMAN_PACED_FALLBACK_STATUS" \
  BASELANE_UTILITY_STATUS="$UTILITY_STATUS" \
  BASELANE_TOKENOMICS_STATUS="$TOKENOMICS_STATUS" \
  BASELANE_NO_DAO_MORTGAGE_CASH_BASIS_STATUS="$NO_DAO_MORTGAGE_CASH_BASIS_STATUS" \
  BASELANE_HEMLANE_LIVE_STATUS="$HEMLANE_LIVE_STATUS" \
  BASELANE_HEMLANE_AUTO_TAG_STATUS="$HEMLANE_AUTO_TAG_STATUS" \
  BASELANE_HEMLANE_AUTO_TAG_APPLIED_COUNT="$HEMLANE_AUTO_TAG_APPLIED_COUNT" \
  BASELANE_HEMLANE_AUTO_TAG_REEXPORT_STATUS="$HEMLANE_AUTO_TAG_REEXPORT_STATUS" \
  BASELANE_WEEKLY_STATUS="$WEEKLY_STATUS" \
  BASELANE_MONTHLY_STATEMENTS_STATUS="$MONTHLY_STATEMENTS_STATUS" \
  BASELANE_LOCAL_MODEL_PREFLIGHT_STATUS="$LOCAL_MODEL_PREFLIGHT_STATUS" \
  BASELANE_SOURCE_CASH_STATUS="$SOURCE_CASH_STATUS" \
  BASELANE_YHOME_OPERATING_CASH_STATUS="$YHOME_OPERATING_CASH_STATUS" \
  BASELANE_DISK_PREFLIGHT_STATUS="$DISK_PREFLIGHT_STATUS" \
  BASELANE_DISK_PREFLIGHT_ATTEMPTS="$DISK_PREFLIGHT_ATTEMPTS" \
  BASELANE_DISK_PREFLIGHT_REPORT_FILE="$DISK_PREFLIGHT_REPORT_FILE" \
  BASELANE_DISK_PREFLIGHT_MIN_FREE_MIB="$BASELANE_DISK_PREFLIGHT_MIN_FREE_MIB" \
  BASELANE_STALE_FINANCIAL_ARTIFACT_GUARD_STATUS="$STALE_FINANCIAL_ARTIFACT_GUARD_STATUS" \
  BASELANE_STALE_FINANCIAL_ARTIFACT_GUARD_FILE="$STALE_FINANCIAL_ARTIFACT_GUARD_FILE" \
  BASELANE_GIT_PUSH_STATUS="$GIT_PUSH_STATUS" \
  $PY - <<'PY' || true
import json
import os
from pathlib import Path

report_file = Path(os.environ["BASELANE_RUN_REPORT_FILE"])
history_file = Path(os.environ["BASELANE_RUN_HISTORY_FILE"])
root = Path(os.environ.get("ROOT", "."))
sync_report_file = root / "reports" / "baselane_sync_cdp_report.json"
sync_report = {}
if sync_report_file.exists():
    try:
        sync_report = json.loads(sync_report_file.read_text(encoding="utf-8"))
    except Exception as exc:
        sync_report = {"status": "unreadable", "reason": str(exc)}
sync_step_status = os.environ.get("BASELANE_SYNC_STATUS")
raw_sync_report_status = sync_report.get("status")
effective_sync_report_status = raw_sync_report_status
sync_report_status_source = "child_report"
if sync_step_status and sync_step_status != "ok":
    if sync_step_status == "not_started":
        effective_sync_report_status = "not_started"
        sync_report_status_source = "wrapper_step_status"
    elif raw_sync_report_status in {None, "", "ok"}:
        effective_sync_report_status = sync_step_status
        sync_report_status_source = "wrapper_step_status"
started_epoch = int(os.environ["BASELANE_RUN_STARTED_EPOCH"])
ended_epoch = int(os.environ["BASELANE_RUN_ENDED_EPOCH"])
step_statuses = {
    "session_seed": os.environ.get("BASELANE_SESSION_SEED_STATUS"),
    "auth_preflight": os.environ.get("BASELANE_AUTH_PREFLIGHT_STATUS"),
    "deterministic_sync": os.environ.get("BASELANE_SYNC_STATUS"),
    "human_paced_sync_fallback": os.environ.get("BASELANE_HUMAN_PACED_FALLBACK_STATUS"),
    "utility_overage": os.environ.get("BASELANE_UTILITY_STATUS"),
    "tokenomics": os.environ.get("BASELANE_TOKENOMICS_STATUS"),
    "no_dao_mortgage_cash_basis": os.environ.get("BASELANE_NO_DAO_MORTGAGE_CASH_BASIS_STATUS"),
    "hemlane_live_transaction_evidence": os.environ.get("BASELANE_HEMLANE_LIVE_STATUS"),
    "hemlane_auto_tag_source_fix": os.environ.get("BASELANE_HEMLANE_AUTO_TAG_STATUS"),
    "hemlane_auto_tag_reexport": os.environ.get("BASELANE_HEMLANE_AUTO_TAG_REEXPORT_STATUS"),
    "weekly_unprocessed_pass": os.environ.get("BASELANE_WEEKLY_STATUS"),
    "monthly_statement_staging": os.environ.get("BASELANE_MONTHLY_STATEMENTS_STATUS"),
    "local_model_preflight": os.environ.get("BASELANE_LOCAL_MODEL_PREFLIGHT_STATUS"),
    "source_cash_balance_audit": os.environ.get("BASELANE_SOURCE_CASH_STATUS"),
    "yhome_operating_cash_sync": os.environ.get("BASELANE_YHOME_OPERATING_CASH_STATUS"),
    "disk_space_preflight": os.environ.get("BASELANE_DISK_PREFLIGHT_STATUS"),
    "stale_financial_artifact_guard": os.environ.get("BASELANE_STALE_FINANCIAL_ARTIFACT_GUARD_STATUS"),
    "assetrail_git_push": os.environ.get("BASELANE_GIT_PUSH_STATUS"),
}
human_paced_backup_script = root / "scripts" / "baselane_sync_cdp_human_paced.py"
human_paced_backup_enabled = os.environ.get("BASELANE_CRON_HUMAN_PACED_FALLBACK", "1") == "1"
reported_status = os.environ["BASELANE_RUN_STATUS"]
reported_return_code = int(os.environ["BASELANE_RUN_RC"])
reported_failed_step = os.environ.get("BASELANE_FAILED_STEP") or None
effective_status = reported_status
effective_return_code = reported_return_code
effective_failed_step = reported_failed_step
deterministic_sync_original_status = os.environ.get("BASELANE_SYNC_ORIGINAL_STATUS") or step_statuses.get("deterministic_sync")
deterministic_sync_recovery_status = os.environ.get("BASELANE_SYNC_RECOVERY_STATUS") or "not_required"
deterministic_sync_recovered_by = os.environ.get("BASELANE_SYNC_RECOVERED_BY") or None
deterministic_sync_recovery_report = os.environ.get("BASELANE_SYNC_RECOVERY_REPORT") or None
deterministic_sync_recovery_required = deterministic_sync_recovery_status not in {"", "not_required"}
wrapper_consistency_issues = []
if reported_status == "ok":
    if step_statuses.get("deterministic_sync") != "ok":
        wrapper_consistency_issues.append(
            f"wrapper_ok_but_deterministic_sync={step_statuses.get('deterministic_sync') or 'missing'}"
        )
    if not human_paced_backup_enabled:
        wrapper_consistency_issues.append("wrapper_ok_but_human_paced_backup_disabled")
    if not human_paced_backup_script.is_file():
        wrapper_consistency_issues.append("wrapper_ok_but_human_paced_backup_script_missing")
    if os.environ.get("BASELANE_ASSETRAIL_PUSH_ENABLED", "0") == "1" and step_statuses.get("assetrail_git_push") not in {"committed_and_pushed", "pushed_no_ledger_changes"}:
        wrapper_consistency_issues.append(
            f"wrapper_ok_but_assetrail_git_push={step_statuses.get('assetrail_git_push') or 'missing'}"
        )
    if effective_sync_report_status not in {None, "", "ok"}:
        wrapper_consistency_issues.append(
            f"wrapper_ok_but_sync_report_status={effective_sync_report_status}"
        )
if wrapper_consistency_issues:
    effective_status = "review"
    effective_return_code = max(1, reported_return_code)
    if step_statuses.get("deterministic_sync") != "ok" or effective_sync_report_status not in {None, "", "ok"}:
        effective_failed_step = "baselane_sync_cdp_deterministic"
    elif os.environ.get("BASELANE_ASSETRAIL_PUSH_ENABLED", "0") == "1" and step_statuses.get("assetrail_git_push") not in {"committed_and_pushed", "pushed_no_ledger_changes"}:
        effective_failed_step = "assetrail_git_push"
report = {
    "job": "baselane-daily-sync",
    "generated_at": os.environ["BASELANE_RUN_ENDED_AT"],
    "status": effective_status,
    "return_code": effective_return_code,
    "failed_step": effective_failed_step,
    "reported_status": reported_status,
    "reported_return_code": reported_return_code,
    "reported_failed_step": reported_failed_step,
    "wrapper_consistency_issues": wrapper_consistency_issues,
    "deterministic_sync_original_status": deterministic_sync_original_status,
    "deterministic_sync_recovery_required": deterministic_sync_recovery_required,
    "deterministic_sync_recovery_status": deterministic_sync_recovery_status,
    "deterministic_sync_recovered_by": deterministic_sync_recovered_by,
    "deterministic_sync_recovery_report": deterministic_sync_recovery_report,
    "human_paced_backup_policy": "deterministic_primary_human_paced_backup",
    "human_paced_backup_enabled": human_paced_backup_enabled,
    "human_paced_backup_script": str(human_paced_backup_script),
    "human_paced_backup_script_exists": human_paced_backup_script.is_file(),
    "started_at": os.environ["BASELANE_RUN_STARTED_AT"],
    "ended_at": os.environ["BASELANE_RUN_ENDED_AT"],
    "finished_at": os.environ["BASELANE_RUN_ENDED_AT"],
    "duration_seconds": max(0, ended_epoch - started_epoch),
    "workspace_root": os.environ.get("ROOT") or os.environ.get("WORKSPACE_ROOT"),
    "openclaw_root": os.environ.get("OPENCLAW_ROOT"),
    "dropbox_root": os.environ.get("DROPBOX_ROOT"),
    "baselane_ledger_path": os.environ.get("BASELANE_LEDGER_PATH"),
    "disk_space_preflight_min_free_mib": int(os.environ.get("BASELANE_DISK_PREFLIGHT_MIN_FREE_MIB") or 0),
    "disk_space_preflight_attempts": int(os.environ.get("BASELANE_DISK_PREFLIGHT_ATTEMPTS") or 0),
    "cdp_version_url": os.environ.get("BASELANE_CDP_VERSION_URL"),
    "auth_preflight_status": os.environ.get("BASELANE_AUTH_PREFLIGHT_STATUS"),
    "sync_report_status": effective_sync_report_status,
    "sync_report_raw_status": raw_sync_report_status,
    "sync_report_status_source": sync_report_status_source,
    "sync_report_reason": sync_report.get("reason"),
    "sync_report_failure_class": sync_report.get("export_failure_class"),
    "steps": step_statuses,
    "artifacts": {
        "latest_report": str(report_file),
        "history": str(history_file),
	        "sync_report": str(sync_report_file),
	        "scope_guard": str(root / "reports" / "baselane_daily_scope_guard.json"),
        "local_model_preflight_report": str(root / "reports" / "baselane_local_model_preflight_report.json"),
        "auth_preflight_report": str(root / "reports" / "baselane_cdp_auth_recovery_report.json"),
        "disk_space_preflight_report": os.environ.get("BASELANE_DISK_PREFLIGHT_REPORT_FILE"),
        "stale_financial_artifact_guard": os.environ.get("BASELANE_STALE_FINANCIAL_ARTIFACT_GUARD_FILE"),
        "hemlane_live_transactions": str(root / "reports" / "hemlane_live_transactions.json"),
        "hemlane_auto_tag_report": str(root / "reports" / "baselane_hemlane_auto_tag_report.json"),
        "utility_overage_report": str(root / "reports" / "utility_overage_report.csv"),
        "weekly_file_updates_report": str(root / "reports" / "baselane_weekly_file_updates_run_report.json"),
        "weekly_unprocessed_report": str(root / "reports" / "baselane_weekly_unprocessed_report.json"),
    },
    "hemlane_auto_tag_applied_count": int(os.environ.get("BASELANE_HEMLANE_AUTO_TAG_APPLIED_COUNT") or 0),
}
tmp = report_file.with_suffix(report_file.suffix + ".tmp")
tmp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(report_file)
with history_file.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(report, sort_keys=True) + "\n")
PY
  if [ -f "$ROOT/scripts/baselane_daily_sync_report.py" ]; then
    $PY "$ROOT/scripts/baselane_daily_sync_report.py" --root "$ROOT" --report "$SYNC_REPORT_FILE" >/dev/null || true
  fi
}

finish_run_report() {
  local rc="$?"
  local report_rc="$rc"
  local status="ok"
  local failed_step=""
  if [ -n "$INTERRUPT_SIGNAL" ]; then
    status="interrupted"
    failed_step="$CURRENT_STEP"
    report_rc="$rc"
    if [ "$report_rc" -eq 0 ]; then
      report_rc="130"
    fi
  elif [ "$rc" -eq 0 ]; then
    if [ "$LOCAL_MODEL_PREFLIGHT_STATUS" = "in_progress" ]; then
      status="failed"
      failed_step="baselane_local_model_preflight"
    elif [ "$DISK_PREFLIGHT_STATUS" = "in_progress" ]; then
      status="failed"
      failed_step="baselane_disk_space_preflight"
    elif [ "$SESSION_SEED_STATUS" = "in_progress" ]; then
      status="failed"
      failed_step="baselane_seed_session"
    elif [ "$SYNC_STATUS" = "in_progress" ]; then
      status="failed"
      failed_step="baselane_sync_cdp_deterministic"
    elif [ "$HEMLANE_AUTO_TAG_STATUS" = "in_progress" ]; then
      status="failed"
      failed_step="baselane_hemlane_auto_tag_source_fix"
    elif [ "$GIT_PUSH_STATUS" = "in_progress" ]; then
      status="failed"
      failed_step="assetrail_git_push"
    elif [ "$LOCAL_MODEL_PREFLIGHT_STATUS" = "not_started" ]; then
      status="failed"
      failed_step="baselane_local_model_preflight"
    elif [ "$DISK_PREFLIGHT_STATUS" = "not_started" ]; then
      status="failed"
      failed_step="baselane_disk_space_preflight"
    elif [ "$SYNC_STATUS" = "not_started" ]; then
      status="failed"
      failed_step="baselane_sync_cdp_deterministic"
    elif [ "$DISK_PREFLIGHT_STATUS" != "ok" ]; then
      status="review"
      failed_step="baselane_disk_space_preflight"
    elif [ "$STALE_FINANCIAL_ARTIFACT_GUARD_STATUS" != "ok" ]; then
      status="review"
      failed_step="stale_financial_artifact_guard"
    elif [ "$SYNC_STATUS" != "ok" ]; then
      status="review"
      failed_step="baselane_sync_cdp_deterministic"
    elif [ "$HEMLANE_AUTO_TAG_STATUS" != "ok" ] && [ "$HEMLANE_AUTO_TAG_STATUS" != "skipped_by_env" ]; then
      status="review"
      failed_step="baselane_hemlane_auto_tag_source_fix"
    elif [ "$ASSETRAIL_PUSH_ENABLED" = "1" ] && [ "$GIT_PUSH_STATUS" != "committed_and_pushed" ] && [ "$GIT_PUSH_STATUS" != "pushed_no_ledger_changes" ]; then
      status="review"
      failed_step="assetrail_git_push"
    fi
  fi
  if [ "$rc" -eq 0 ] && [ "$status" = "ok" ]; then
    write_run_report "$rc" "ok" ""
  elif [ "$rc" -eq 0 ]; then
    report_rc="1"
    write_run_report "$report_rc" "$status" "$failed_step"
  else
    write_run_report "$rc" "failed" "$CURRENT_STEP"
  fi
  exit "$report_rc"
}
trap finish_run_report EXIT
handle_interrupt() {
  local signal="$1"
  if [ "$CURRENT_STEP" = "baselane_daily_source_cash_balance" ]; then
    write_source_cash_timeout_report "source_cash_audit_interrupted_${signal}"
  fi
  INTERRUPT_SIGNAL="$signal"
  if [ "$signal" = "TERM" ]; then
    echo "[baselane] SIGTERM received at step=$CURRENT_STEP; checkpointing..." >&2
    exit 143
  fi
  exit 130
}
trap 'handle_interrupt "INT"' INT
trap 'handle_interrupt "TERM"' TERM

CURRENT_STEP="baselane_scope_guard"
soft_timeout_check
SCOPE_GUARD_SCRIPT="$ROOT/scripts/baselane_scope_guard.py"
if [ -f "$SCOPE_GUARD_SCRIPT" ]; then
  scope_guard_cmd=(
    "$PY" "$SCOPE_GUARD_SCRIPT"
    --path "dropbox_root=$DROPBOX_ROOT"
    --path "baselane_ledger_path=$BASELANE_LEDGER_PATH"
    --report "$SCOPE_GUARD_FILE"
  )
  if [ "${ALLOW_BASELANE_STAGING_ROOT:-0}" = "1" ]; then
    scope_guard_cmd+=(--allow-staging)
  fi
  set +e
  "${scope_guard_cmd[@]}" >/dev/null
  scope_guard_rc="$?"
  set -e
  if [ "$scope_guard_rc" -ne 0 ]; then
    echo "[baselane-daily] refusing invalid financial source path for Baselane automation; see $SCOPE_GUARD_FILE" >&2
    exit "$scope_guard_rc"
  fi
fi

if [ -z "${BW_ENV:-}" ]; then
  export BW_ENV="$ROOT/.secrets/bw.env"
fi

CURRENT_STEP="baselane_disk_space_preflight"
soft_timeout_check
DISK_PREFLIGHT_STATUS="in_progress"
if [ -x "$ROOT/scripts/baselane_disk_space_preflight.py" ]; then
  run_disk_preflight() {
    timeout --kill-after="${BASELANE_DISK_PREFLIGHT_KILL_AFTER_SECONDS}s" \
      "${BASELANE_DISK_PREFLIGHT_TIMEOUT_SECONDS}s" \
      "$PY" "$ROOT/scripts/baselane_disk_space_preflight.py" \
      --path "dropbox_root=$DROPBOX_ROOT" \
      --path "baselane_ledger_dir=$BASELANE_LEDGER_DIR" \
      --path "workspace_root=$ROOT" \
      --min-free-mib "$BASELANE_DISK_PREFLIGHT_MIN_FREE_MIB" \
      --path-timeout-seconds "$BASELANE_DISK_PREFLIGHT_PATH_TIMEOUT_SECONDS" \
      --report "$DISK_PREFLIGHT_REPORT_FILE" >/dev/null
  }
  set +e
  DISK_PREFLIGHT_ATTEMPTS="1"
  run_disk_preflight
  disk_preflight_rc="$?"
  if [ "$disk_preflight_rc" -ne 0 ]; then
    echo "[baselane-daily] disk preflight attempt 1 failed (rc=$disk_preflight_rc); retrying once before blocking sync" >&2
    sleep "$BASELANE_DISK_PREFLIGHT_RETRY_DELAY_SECONDS"
    DISK_PREFLIGHT_ATTEMPTS="2"
    run_disk_preflight
    disk_preflight_rc="$?"
  fi
  set -e
  if [ "$disk_preflight_rc" -eq 0 ]; then
    DISK_PREFLIGHT_STATUS="ok"
  else
    DISK_PREFLIGHT_STATUS="failed_rc_${disk_preflight_rc}"
    echo "[baselane-daily] refusing Baselane sync while free disk space is below threshold; see $DISK_PREFLIGHT_REPORT_FILE" >&2
    exit "$disk_preflight_rc"
  fi
else
  DISK_PREFLIGHT_STATUS="skipped_missing_script"
fi

CURRENT_STEP="stale_financial_artifact_guard"
STALE_FINANCIAL_ARTIFACT_GUARD_SCRIPT="$ROOT/scripts/baselane_stale_financial_artifact_guard.py"
if [ ! -f "$STALE_FINANCIAL_ARTIFACT_GUARD_SCRIPT" ]; then
  STALE_FINANCIAL_ARTIFACT_GUARD_STATUS="failed_missing_script"
  echo "[baselane-daily] missing stale financial artifact guard: $STALE_FINANCIAL_ARTIFACT_GUARD_SCRIPT" >&2
  exit 1
fi
set +e
timeout --kill-after=15s "${STALE_FINANCIAL_ARTIFACT_GUARD_TIMEOUT_SECONDS}s" "$PY" "$STALE_FINANCIAL_ARTIFACT_GUARD_SCRIPT" \
  --root "$DROPBOX_ROOT/Real Estate" --report "$STALE_FINANCIAL_ARTIFACT_GUARD_FILE" >/dev/null
stale_financial_artifact_guard_rc="$?"
set -e
if [ "$stale_financial_artifact_guard_rc" -eq 0 ]; then
  STALE_FINANCIAL_ARTIFACT_GUARD_STATUS="ok"
elif [ "$stale_financial_artifact_guard_rc" -eq 2 ]; then
  STALE_FINANCIAL_ARTIFACT_GUARD_STATUS="review"
  echo "[baselane-daily] refusing ledger export because stale financial artifacts remain; see $STALE_FINANCIAL_ARTIFACT_GUARD_FILE" >&2
  exit 0
else
  STALE_FINANCIAL_ARTIFACT_GUARD_STATUS="failed"
  echo "[baselane-daily] stale financial artifact guard failed (rc=$stale_financial_artifact_guard_rc)" >&2
  exit "$stale_financial_artifact_guard_rc"
fi

# ── Cyber Windows Brave CDP ──────────────────────────────────────────────────
# Prefer an already reachable local Brave CDP endpoint. Fall back to Cyber's
# Tailscale-exposed CDP alias for Umbrel/remote execution.
configure_baselane_cdp

# ── Deterministic Baselane export (Python CDP — no pagination bugs) ──────────
# Requires: Brave running with remote debugging on Cyber Windows.
# Environment: BASELANE_CDP_VERSION_URL is probed above and re-resolved by
# baselane_brave_utils.py when not set.
# The Python script finds the active Brave tab, exports all transactions in one
# CDP call, then runs the split pipeline.
# Self-heal: ensure an authenticated Baselane CDP session before the export.
# No-op if session valid; re-seeds via Bitwarden creds if expired. Non-fatal: a
# seed failure leaves the export to 401 as before (does not abort the cron).
CURRENT_STEP="baselane_local_model_preflight"
soft_timeout_check
if [ -x "$ROOT/scripts/baselane_local_model_preflight.py" ]; then
  LOCAL_MODEL_PREFLIGHT_STATUS="in_progress"
  local_model_preflight_timeout_seconds="${BASELANE_LOCAL_MODEL_PREFLIGHT_TIMEOUT_SECONDS:-240}"
  set +e
  timeout --kill-after=30s "${local_model_preflight_timeout_seconds}s" \
    "$ROOT/scripts/baselane_local_model_preflight.py" --json >/dev/null
  local_model_preflight_rc="$?"
  set -e
  if [ "$local_model_preflight_rc" -eq 0 ]; then
    LOCAL_MODEL_PREFLIGHT_STATUS="ok"
  elif [ "$local_model_preflight_rc" -eq 124 ]; then
    LOCAL_MODEL_PREFLIGHT_STATUS="review_timeout_nonfatal_${local_model_preflight_timeout_seconds}s"
    echo "[baselane] local model preflight timed out after ${local_model_preflight_timeout_seconds}s; continuing with deterministic pipeline" >&2
  else
    LOCAL_MODEL_PREFLIGHT_STATUS="review_nonfatal"
  fi
else
  LOCAL_MODEL_PREFLIGHT_STATUS="skipped_missing_script"
fi

CURRENT_STEP="baselane_auth_preflight"
soft_timeout_check
AUTH_PREFLIGHT_STATUS="in_progress"
AUTH_PREFLIGHT_REPORT="$ROOT/reports/baselane_cdp_auth_recovery_report.json"
auth_preflight_timeout_seconds="${BASELANE_AUTH_PREFLIGHT_TIMEOUT_SECONDS:-45}"
set +e
timeout --kill-after=5s "${auth_preflight_timeout_seconds}s" \
  "$PY" "$ROOT/scripts/baselane_cdp_auth_recovery.py" \
  --cdp-url "$BASELANE_CDP_VERSION_URL" \
  --graphql-auth-smoke \
  --recovery-wait-seconds 0 \
  --report "$AUTH_PREFLIGHT_REPORT" >/dev/null
auth_preflight_rc="$?"
set -e
if [ "$auth_preflight_rc" -eq 0 ]; then
  AUTH_PREFLIGHT_STATUS="ok"
else
  CURRENT_STEP="baselane_seed_session"
  soft_timeout_check
  seed_timeout_seconds="${BASELANE_SESSION_SEED_TIMEOUT_SECONDS:-180}"
  SESSION_SEED_STATUS="in_progress"
  if "$ROOT/scripts/baselane_seed_session.sh"; then
    SESSION_SEED_STATUS="ok"
  else
    seed_rc="$?"
    if [ "$seed_rc" -eq 124 ]; then
      SESSION_SEED_STATUS="timeout_nonfatal_${seed_timeout_seconds}s"
      echo "[baselane] session seed timed out after ${seed_timeout_seconds}s; continuing" >&2
    else
      SESSION_SEED_STATUS="failed_nonfatal"
      echo "[baselane] session seed failed (rc=$seed_rc); continuing" >&2
    fi
  fi

  CURRENT_STEP="baselane_auth_preflight"
  soft_timeout_check
  AUTH_PREFLIGHT_STATUS="recovery_in_progress"
  set +e
  timeout --kill-after=5s "${auth_preflight_timeout_seconds}s" \
    "$PY" "$ROOT/scripts/baselane_cdp_auth_recovery.py" \
    --cdp-url "$BASELANE_CDP_VERSION_URL" \
    --recover-login \
    --graphql-auth-smoke \
    --recovery-wait-seconds 0 \
    --report "$AUTH_PREFLIGHT_REPORT" >/dev/null
  auth_preflight_rc="$?"
  set -e
  if [ "$auth_preflight_rc" -eq 0 ]; then
    AUTH_PREFLIGHT_STATUS="ok_recovered"
  elif [ "$auth_preflight_rc" -eq 124 ]; then
    AUTH_PREFLIGHT_STATUS="failed_timeout_${auth_preflight_timeout_seconds}s"
  else
    AUTH_PREFLIGHT_STATUS="failed_rc_${auth_preflight_rc}"
  fi
fi
if [[ "$AUTH_PREFLIGHT_STATUS" != ok* ]]; then
  echo "[baselane] refusing ledger export: authenticated Baselane content was not verified; see $AUTH_PREFLIGHT_REPORT" >&2
  exit 1
fi
if [ "$SESSION_SEED_STATUS" = "not_started" ]; then
  SESSION_SEED_STATUS="skipped_existing_authenticated_session"
fi

NO_DAO_MORTGAGE_CASH_BASIS="$ROOT/scripts/baselane_no_dao_mortgage_cash_basis.py"
if [ "${BASELANE_CRON_SKIP_NO_DAO_MORTGAGE_CASH_BASIS:-0}" = "1" ]; then
  NO_DAO_MORTGAGE_CASH_BASIS_STATUS="skipped_by_env"
elif [ -f "$NO_DAO_MORTGAGE_CASH_BASIS" ]; then
  CURRENT_STEP="baselane_no_dao_mortgage_cash_basis_preview"
  "$PY" "$NO_DAO_MORTGAGE_CASH_BASIS" >/dev/null
  no_dao_action_count="$("$PY" -c 'import json,sys; print(int(json.load(open(sys.argv[1])).get("action_count") or 0))' "$ROOT/reports/no_dao_mortgage_cash_basis_live.json")"
  if [ "$no_dao_action_count" -gt 0 ]; then
    no_dao_digest="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["action_digest"])' "$ROOT/reports/no_dao_mortgage_cash_basis_live.json")"
    CURRENT_STEP="baselane_no_dao_mortgage_cash_basis_apply"
    "$PY" "$NO_DAO_MORTGAGE_CASH_BASIS" \
      --apply \
      --require-action-digest "$no_dao_digest" >/dev/null
    NO_DAO_MORTGAGE_CASH_BASIS_STATUS="ok_applied_${no_dao_action_count}"
  else
    NO_DAO_MORTGAGE_CASH_BASIS_STATUS="ok_no_actions"
  fi
else
  echo "[baselane] required no-DAO mortgage cash-basis reconciler is missing" >&2
  exit 1
fi

CURRENT_STEP="baselane_sync_cdp_deterministic"
soft_timeout_check
SYNC_STATUS="in_progress"
set +e
"$PY" "$ROOT/scripts/baselane_sync_cdp_deterministic.py"
sync_rc="$?"
set -e
if [ "$sync_rc" -eq 0 ]; then
  SYNC_STATUS="ok"
elif [ "$sync_rc" -eq 2 ]; then
  # Check if the guard failed with 0 rows - if so, treat as failed and trigger human-paced fallback
  GUARD_FILE="$ROOT/reports/baselane_export_guard_last.json"
  GUARD_ROWS=0
  if [ -f "$GUARD_FILE" ]; then
    GUARD_ROWS=$(python3 -c "import json;d=json.load(open('$GUARD_FILE'));print(d.get('output_rows',0))" 2>/dev/null || echo 0)
  fi
  if [ "$GUARD_ROWS" -lt 100 ]; then
    echo "[baselane] deterministic CDP sync returned review with ${GUARD_ROWS} rows; triggering human-paced fallback" >&2
    SYNC_ORIGINAL_STATUS="review_nonfatal_${GUARD_ROWS}_rows"
    HUMAN_PACED_SYNC="$ROOT/scripts/baselane_sync_cdp_human_paced.py"
    if [ "${BASELANE_CRON_HUMAN_PACED_FALLBACK:-1}" = "1" ] && [ -f "$HUMAN_PACED_SYNC" ]; then
      CURRENT_STEP="baselane_sync_cdp_human_paced_fallback"
      HUMAN_PACED_FALLBACK_STATUS="in_progress"
      echo "[baselane] attempting human-paced fallback for 0-row export" >&2
      set +e
      BASELANE_ENABLE_LEGACY_HUMAN_PACED=1 "$PY" "$HUMAN_PACED_SYNC"
      human_paced_rc="$?"
      set -e
      if [ "$human_paced_rc" -eq 0 ]; then
        SYNC_STATUS="ok"
        HUMAN_PACED_FALLBACK_STATUS="ok"
        SYNC_RECOVERY_STATUS="recovered_by_human_paced_fallback"
        SYNC_RECOVERED_BY="baselane_sync_cdp_human_paced"
        SYNC_RECOVERY_REPORT="$ROOT/reports/baselane_sync_cdp_report.json"
        echo "[baselane] human-paced fallback recovered 0-row deterministic sync" >&2
      else
        SYNC_STATUS="review_nonfatal_human_paced_rc_${human_paced_rc}"
        HUMAN_PACED_FALLBACK_STATUS="failed_rc_${human_paced_rc}"
        SYNC_RECOVERY_STATUS="human_paced_fallback_failed"
        SYNC_RECOVERED_BY="baselane_sync_cdp_human_paced"
        SYNC_RECOVERY_REPORT="$ROOT/reports/baselane_sync_cdp_report.json"
        echo "[baselane] human-paced fallback failed rc=$human_paced_rc; continuing with review status" >&2
      fi
    else
      SYNC_STATUS="review_nonfatal"
      echo "[baselane] human-paced fallback disabled; continuing with review status" >&2
    fi
  else
    SYNC_STATUS="review_nonfatal"
    echo "[baselane] deterministic CDP sync returned review with ${GUARD_ROWS} rows; continuing to refresh local audits" >&2
  fi
else
  SYNC_ORIGINAL_STATUS="failed_rc_${sync_rc}"
  HUMAN_PACED_SYNC="$ROOT/scripts/baselane_sync_cdp_human_paced.py"
  if [ "${BASELANE_CRON_HUMAN_PACED_FALLBACK:-1}" = "1" ] && [ -f "$HUMAN_PACED_SYNC" ]; then
    CURRENT_STEP="baselane_sync_cdp_human_paced_fallback"
    HUMAN_PACED_FALLBACK_STATUS="in_progress"
    echo "[baselane] deterministic CDP sync failed rc=$sync_rc; attempting human-paced fallback" >&2
    set +e
    BASELANE_ENABLE_LEGACY_HUMAN_PACED=1 "$PY" "$HUMAN_PACED_SYNC"
    human_paced_rc="$?"
    set -e
    if [ "$human_paced_rc" -eq 0 ]; then
      SYNC_STATUS="ok"
      HUMAN_PACED_FALLBACK_STATUS="ok"
      SYNC_RECOVERY_STATUS="recovered_by_human_paced_fallback"
      SYNC_RECOVERED_BY="baselane_sync_cdp_human_paced"
      SYNC_RECOVERY_REPORT="$ROOT/reports/baselane_sync_cdp_report.json"
      echo "[baselane] human-paced fallback recovered deterministic sync failure rc=$sync_rc" >&2
    else
      SYNC_STATUS="failed_rc_${sync_rc}_human_paced_rc_${human_paced_rc}"
      HUMAN_PACED_FALLBACK_STATUS="failed_rc_${human_paced_rc}"
      SYNC_RECOVERY_STATUS="human_paced_fallback_failed"
      SYNC_RECOVERED_BY="baselane_sync_cdp_human_paced"
      SYNC_RECOVERY_REPORT="$ROOT/reports/baselane_sync_cdp_report.json"
      echo "[baselane] human-paced fallback failed rc=$human_paced_rc; continuing fail-closed evidence gates without source writes" >&2
    fi
  else
    SYNC_STATUS="failed_rc_${sync_rc}"
    SYNC_RECOVERY_STATUS="human_paced_fallback_disabled"
    HUMAN_PACED_FALLBACK_STATUS="disabled"
    echo "[baselane] human-paced fallback disabled; continuing fail-closed evidence gates without source writes" >&2
  fi
fi

CURRENT_STEP="hemlane_live_transaction_evidence"
soft_timeout_check
if [ "${BASELANE_HEMLANE_AUTO_TAG_ENABLED:-1}" = "1" ] && [ -f "$ROOT/scripts/baselane_hemlane_live_transactions.py" ]; then
  HEMLANE_LIVE_STATUS="in_progress"
  set +e
  "$PY" "$ROOT/scripts/baselane_hemlane_live_transactions.py" \
    --root "$ROOT" \
    --report "$ROOT/reports/hemlane_live_transactions.json" >/dev/null
  hemlane_live_rc="$?"
  set -e
  if [ "$hemlane_live_rc" -ne 0 ]; then
    HEMLANE_LIVE_STATUS="review_nonfatal_rc_${hemlane_live_rc}"
    echo "[baselane] Hemlane live transaction evidence unavailable rc=$hemlane_live_rc; source-fix evidence will fail closed" >&2
  else
    HEMLANE_LIVE_STATUS="ok"
  fi
elif [ "${BASELANE_HEMLANE_AUTO_TAG_ENABLED:-1}" != "1" ]; then
  HEMLANE_LIVE_STATUS="skipped_by_env"
else
  HEMLANE_LIVE_STATUS="skipped_missing_script"
fi

CURRENT_STEP="baselane_daily_source_cash_balance"
soft_timeout_check
run_source_cash_balance_audit

CURRENT_STEP="baselane_daily_yhome_operating_cash_sync"
soft_timeout_check
run_daily_yhome_operating_cash_sync

CURRENT_STEP="baselane_hemlane_auto_tag_source_fix"
soft_timeout_check
if [ "${BASELANE_HEMLANE_AUTO_TAG_ENABLED:-1}" = "1" ] && [ -x "$ROOT/scripts/baselane_hemlane_auto_tag_source_fix.sh" ]; then
  HEMLANE_AUTO_TAG_STATUS="in_progress"
  set +e
  if [ "$SYNC_STATUS" = "ok" ]; then
    "$ROOT/scripts/baselane_hemlane_auto_tag_source_fix.sh" >/dev/null
  else
    BASELANE_HEMLANE_AUTO_TAG_APPLY=0 "$ROOT/scripts/baselane_hemlane_auto_tag_source_fix.sh" >/dev/null
  fi
  hemlane_auto_tag_rc="$?"
  set -e
  HEMLANE_AUTO_TAG_APPLIED_COUNT="$("$PY" - "$ROOT/reports/baselane_hemlane_auto_tag_report.json" <<'PY'
import json
import sys
from pathlib import Path
try:
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except Exception:
    data = {}
print(int(data.get("applied_count") or 0))
PY
)"
  if [ "$hemlane_auto_tag_rc" -ne 0 ]; then
    HEMLANE_AUTO_TAG_STATUS="review_nonfatal_rc_${hemlane_auto_tag_rc}"
    echo "[baselane] Hemlane auto-tag source fix returned rc=$hemlane_auto_tag_rc; daily report will surface the issue" >&2
  elif [ "$SYNC_STATUS" != "ok" ]; then
    HEMLANE_AUTO_TAG_STATUS="dry_run_sync_not_clean"
    echo "[baselane] Hemlane auto-tag source fix ran read-only because Baselane sync is not clean" >&2
  else
    HEMLANE_AUTO_TAG_STATUS="ok"
  fi
elif [ "${BASELANE_HEMLANE_AUTO_TAG_ENABLED:-1}" != "1" ]; then
  HEMLANE_AUTO_TAG_STATUS="skipped_by_env"
else
  HEMLANE_AUTO_TAG_STATUS="skipped_missing_script"
fi

if [ "${HEMLANE_AUTO_TAG_APPLIED_COUNT:-0}" -gt 0 ]; then
  CURRENT_STEP="baselane_hemlane_auto_tag_reexport"
  HEMLANE_AUTO_TAG_REEXPORT_STATUS="in_progress"
  set +e
  "$PY" "$ROOT/scripts/baselane_sync_cdp_deterministic.py"
  hemlane_reexport_rc="$?"
  set -e
  if [ "$hemlane_reexport_rc" -eq 0 ]; then
    HEMLANE_AUTO_TAG_REEXPORT_STATUS="ok"
    SYNC_STATUS="ok"
    run_source_cash_balance_audit
  else
    HEMLANE_AUTO_TAG_REEXPORT_STATUS="failed_rc_${hemlane_reexport_rc}"
    SYNC_STATUS="failed_hemlane_auto_tag_reexport_rc_${hemlane_reexport_rc}"
    echo "[baselane] Hemlane auto-tag applied $HEMLANE_AUTO_TAG_APPLIED_COUNT row(s), but re-export failed rc=$hemlane_reexport_rc; refusing AssetRail push" >&2
  fi
fi

CURRENT_STEP="baselane_pre_push_source_guards"
soft_timeout_check
if [ "$SYNC_STATUS" != "ok" ]; then
  GIT_PUSH_STATUS="skipped_sync_not_clean"
  write_assetrail_push_report "$GIT_PUSH_STATUS" "$SYNC_STATUS"
elif { [ "$HEMLANE_AUTO_TAG_REEXPORT_STATUS" = "not_required" ] || [ "$HEMLANE_AUTO_TAG_REEXPORT_STATUS" = "ok" ]; } && [ "$ASSETRAIL_PUSH_ENABLED" = "1" ]; then
  if run_pre_push_source_guards; then
    CURRENT_STEP="assetrail_git_push"
    GIT_PUSH_STATUS="in_progress"
    push_baselane_ledger_repo
  else
    guard_rc="$?"
    GIT_PUSH_STATUS="skipped_source_guard_failed"
    write_assetrail_push_report "$GIT_PUSH_STATUS" "first_day_pm_fee_source_guard_failed_rc_${guard_rc}"
  fi
else
  if [ "$HEMLANE_AUTO_TAG_REEXPORT_STATUS" = "not_required" ] || [ "$HEMLANE_AUTO_TAG_REEXPORT_STATUS" = "ok" ]; then
    GIT_PUSH_STATUS="skipped_external_push_not_enabled"
    write_assetrail_push_report "$GIT_PUSH_STATUS" "BASELANE_ASSETRAIL_PUSH_ENABLED=0"
  else
    GIT_PUSH_STATUS="skipped_hemlane_auto_tag_reexport_failed"
    write_assetrail_push_report "$GIT_PUSH_STATUS" "$HEMLANE_AUTO_TAG_REEXPORT_STATUS"
  fi
fi

CURRENT_STEP="baselane_first_day_pm_fee_audit"
soft_timeout_check
if [ -f "$ROOT/scripts/baselane_first_day_pm_fee_audit.py" ]; then
  set +e
  "$PY" "$ROOT/scripts/baselane_first_day_pm_fee_audit.py" \
    --gl-csv "$BASELANE_LEDGER_PATH" \
    --all-months \
    --report "$ROOT/reports/baselane_first_day_pm_fee_audit.json" >/dev/null
  first_day_pm_fee_rc="$?"
  set -e
  if [ "$first_day_pm_fee_rc" -ne 0 ]; then
    echo "[baselane] first-day PM fee audit returned rc=$first_day_pm_fee_rc; daily report will surface the issue" >&2
  fi
fi
CURRENT_STEP="baselane_pm_fee_duplicate_lane_audit"
soft_timeout_check
if [ -f "$ROOT/scripts/baselane_pm_fee_duplicate_lane_audit.py" ]; then
  set +e
  "$PY" "$ROOT/scripts/baselane_pm_fee_duplicate_lane_audit.py" \
    --gl-csv "$BASELANE_LEDGER_PATH" \
    --report "$ROOT/reports/baselane_pm_fee_duplicate_lane_audit.json" \
    --csv "$ROOT/reports/baselane_pm_fee_duplicate_lane_audit.csv" >/dev/null
  pm_fee_duplicate_lane_rc="$?"
  set -e
  if [ "$pm_fee_duplicate_lane_rc" -ne 0 ]; then
    echo "[baselane] PM fee duplicate-lane audit returned rc=$pm_fee_duplicate_lane_rc; daily report will surface the issue" >&2
  fi
fi
if [ -f "$ROOT/scripts/baselane_first_day_pm_fee_source_cleanup_plan.py" ]; then
  "$PY" "$ROOT/scripts/baselane_first_day_pm_fee_source_cleanup_plan.py" \
    --gl-csv "$BASELANE_LEDGER_PATH" \
    --all-months \
    --report "$ROOT/reports/baselane_first_day_pm_fee_source_cleanup_plan.json" \
    --actions-csv "$ROOT/reports/baselane_first_day_pm_fee_source_cleanup_actions.csv" \
    --markdown "$ROOT/reports/baselane_first_day_pm_fee_source_cleanup_plan.md" >/dev/null
fi

CURRENT_STEP="utility_overage_tracker"
soft_timeout_check
$PY "$ROOT/scripts/utility_overage_tracker.py" \
  --input "$BASELANE_LEDGER_PATH" \
  --output "$ROOT/reports/utility_overage_report.csv" \
  --baseline-months 12
UTILITY_STATUS="ok"

if $PY -c 'import openpyxl, pypdf' >/dev/null 2>&1; then
  CURRENT_STEP="update_coownership_mortgage_tokenomics"
  set +e
  $PY "$ROOT/scripts/update_coownership_mortgage_tokenomics.py"
  tokenomics_rc="$?"
  set -e
  if [ "$tokenomics_rc" -eq 0 ]; then
    TOKENOMICS_STATUS="ok"
  else
    TOKENOMICS_STATUS="review_nonfatal_rc_${tokenomics_rc}"
    echo "[baselane] Co-Ownership Mortgage Tokenomics update returned rc=$tokenomics_rc; keeping daily Baselane sync status separate" >&2
  fi
else
  TOKENOMICS_STATUS="skipped_missing_python_modules"
  echo "[baselane] Skipping Co-Ownership Mortgage Tokenomics update: Python modules openpyxl/pypdf are not installed" >&2
fi

# Weekly Friday-only advanced bookkeeping pass (runs at most once per ISO week)
WEEKLY_WRAPPER="$ROOT/scripts/baselane_weekly_file_updates_cron.sh"
if [ "${BASELANE_CRON_SKIP_WEEKLY:-0}" = "1" ]; then
  WEEKLY_STATUS="skipped_by_env"
elif [ -x "$WEEKLY_WRAPPER" ]; then
  CURRENT_STEP="baselane_weekly_file_updates"
  set +e
  "$WEEKLY_WRAPPER"
  weekly_rc="$?"
  set -e
  WEEKLY_STATUS="$($PY - "$ROOT/reports/baselane_weekly_file_updates_run_report.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    print("report_missing")
else:
    try:
        print(json.loads(path.read_text(encoding="utf-8")).get("status") or "unknown")
    except Exception:
        print("report_unreadable")
PY
)"
  if [ "$weekly_rc" -ne 0 ]; then
    WEEKLY_STATUS="${WEEKLY_STATUS}_nonfatal_rc_${weekly_rc}"
    echo "[baselane] weekly file updates returned rc=$weekly_rc; keeping daily sync report focused on daily sync health" >&2
  fi
else
  WEEKLY_STATUS="skipped_missing_wrapper"
fi

# ── Monthly statement staging handoff ────────────────────────────────────────
# Do not run the monthly gate from ordinary daily sync by default. Some monthly
# close inputs post mid-month, so the dedicated monthly/mid-month job owns this
# gate. Operators can still opt in explicitly when intentionally reusing the
# daily sync auth/session path.
if [ "${BASELANE_DAILY_RUN_MONTHLY_GATE:-0}" = "1" ]; then
  MONTHLY_STATEMENTS_SCRIPT="$ROOT/scripts/baselane_monthly_statements_idempotent.sh"
  if [ -x "$MONTHLY_STATEMENTS_SCRIPT" ]; then
    read -r PREV_YEAR PREV_MONTH <<EOF
$($PY - <<'PY'
import datetime as dt
now = dt.date.today().replace(day=1)
prev = (now - dt.timedelta(days=1)).replace(day=1)
print(prev.year, prev.month)
PY
)
EOF
    echo "[baselane] Refreshing idempotent monthly statement gate for ${PREV_YEAR}-${PREV_MONTH}..."
    CURRENT_STEP="baselane_monthly_statements_idempotent"
    set +e
    BASELANE_MONTHLY_TARGET_YEAR="$PREV_YEAR" \
      BASELANE_MONTHLY_TARGET_MONTH="$PREV_MONTH" \
      "$MONTHLY_STATEMENTS_SCRIPT"
    monthly_statements_rc="$?"
    set -e
    if [ "$monthly_statements_rc" -eq 0 ]; then
      MONTHLY_STATEMENTS_STATUS="ok"
      echo "[baselane] Monthly statement gate verified for ${PREV_YEAR}-${PREV_MONTH}"
    else
      MONTHLY_STATEMENTS_STATUS="review_nonfatal_rc_${monthly_statements_rc}"
      echo "[baselane] Monthly statement gate returned rc=$monthly_statements_rc; EOD will surface the report" >&2
    fi
  else
    MONTHLY_STATEMENTS_STATUS="skipped_missing_idempotent_script"
    echo "[baselane] Monthly statement idempotent script missing or not executable, skipping" >&2
  fi
else
  MONTHLY_STATEMENTS_STATUS="skipped_not_daily"
fi

# ── Monthly accruals idempotent injection ──────────────────────────────────────
# Runs after successful sync, before git push. Audits prior-month accrual
# coverage only; live creation is owned by the guarded 28th/15th truth refresh.
MONTHLY_ACCRUALS_SCRIPT="$ROOT/scripts/baselane_monthly_accruals_cron.sh"
MONTHLY_ACCRUALS_STATUS="not_started"
if [ -x "$MONTHLY_ACCRUALS_SCRIPT" ]; then
  CURRENT_STEP="baselane_monthly_accruals_idempotent"
  echo "[baselane] Running idempotent monthly accruals audit..."
  set +e
  "$MONTHLY_ACCRUALS_SCRIPT"
  accruals_rc="$?"
  set -e
  if [ "$accruals_rc" -eq 0 ]; then
    MONTHLY_ACCRUALS_STATUS="ok"
    echo "[baselane] Monthly accruals verified successfully"
  else
    MONTHLY_ACCRUALS_STATUS="review_nonfatal_rc_${accruals_rc}"
    echo "[baselane] Monthly accruals returned rc=$accruals_rc; continuing daily sync" >&2
  fi
else
  MONTHLY_ACCRUALS_STATUS="skipped_missing_script"
fi

CURRENT_STEP="complete"
