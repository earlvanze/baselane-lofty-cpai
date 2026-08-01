#!/usr/bin/env bash
set -euo pipefail

OPENCLAW_ROOT="${OPENCLAW_ROOT:-$HOME/.openclaw}"
ROOT="${WORKSPACE_ROOT:-$OPENCLAW_ROOT/workspace}"
PY="${PYTHON_BIN:-python3}"
case "$PY" in
  "/usr/bin/env python3"|"env python3") PY="python3" ;;
esac
NODE="${NODE_BIN:-node}"
DRY_RUN="${DRY_RUN:-0}"
if [ "$DRY_RUN" = "1" ]; then
  export BASELANE_NATIVE_SPLIT_APPLY=0
else
  export BASELANE_NATIVE_SPLIT_APPLY="${BASELANE_NATIVE_SPLIT_APPLY:-1}"
fi
STATE_FILE="${BASELANE_MONTHLY_STATE_FILE:-$ROOT/scripts/.baselane_monthly_statements_state}"
REPORT_DIR="${BASELANE_REPORT_DIR:-$ROOT/reports}"
STATEMENTS_IDEMPOTENT_REPORT="${BASELANE_MONTHLY_STATEMENTS_IDEMPOTENT_REPORT:-$REPORT_DIR/baselane_monthly_statements_idempotent_report.json}"
STATEMENTS_OPERATOR_REPORT="${BASELANE_MONTHLY_STATEMENTS_OPERATOR_REPORT:-$REPORT_DIR/baselane_monthly_statements_operator_report.json}"
STATEMENTS_DOWNLOAD_REPORT="${BASELANE_STATEMENTS_DOWNLOAD_REPORT:-$REPORT_DIR/baselane_statements_download_report.json}"
DISK_PREFLIGHT_REPORT_FILE="${BASELANE_MONTHLY_STATEMENTS_DISK_PREFLIGHT_REPORT:-$REPORT_DIR/baselane_monthly_statements_disk_space_preflight_report.json}"
BASELANE_DISK_PREFLIGHT_MIN_FREE_MIB="${BASELANE_DISK_PREFLIGHT_MIN_FREE_MIB:-10240}"
BASELANE_DISK_PREFLIGHT_TIMEOUT_SECONDS="${BASELANE_DISK_PREFLIGHT_TIMEOUT_SECONDS:-90}"
BASELANE_DISK_PREFLIGHT_KILL_AFTER_SECONDS="${BASELANE_DISK_PREFLIGHT_KILL_AFTER_SECONDS:-15}"
export BASELANE_DISK_PREFLIGHT_TIMEOUT_SECONDS BASELANE_DISK_PREFLIGHT_KILL_AFTER_SECONDS
STATEMENTS_MIN_CAPTURED="${BASELANE_MONTHLY_MIN_CAPTURED_STATEMENTS:-1}"
AUTH_PREFLIGHT_SCRIPT="${BASELANE_AUTH_PREFLIGHT_SCRIPT:-$ROOT/scripts/baselane_cdp_auth_recovery.py}"
AUTH_PREFLIGHT_REPORT="${BASELANE_AUTH_PREFLIGHT_REPORT:-$REPORT_DIR/baselane_auth_report.json}"
AUTH_RECOVERY_SCRIPT="${BASELANE_AUTH_RECOVERY_SCRIPT:-$ROOT/scripts/baselane_cdp_auth_recovery.py}"
AUTH_RECOVERY_REPORT="${BASELANE_AUTH_RECOVERY_REPORT:-$REPORT_DIR/baselane_auth_recovery_report.json}"
AUTH_RECOVERY_ENABLED="${BASELANE_AUTH_RECOVERY_ENABLED:-1}"
OBIE_STATE_FILE="${BASELANE_OBIE_MATCH_STATE_FILE:-$ROOT/scripts/.baselane_obie_property_match_state}"
OBIE_TAG_STATE_FILE="${BASELANE_OBIE_TAG_STATE_FILE:-$ROOT/scripts/.baselane_obie_property_tag_state}"
MONTHLY_SCRIPT="${BASELANE_MONTHLY_SCRIPT:-$ROOT/scripts/baselane_monthly_statements.sh}"
STATEMENTS_OPERATOR_SCRIPT="${BASELANE_STATEMENTS_OPERATOR_SCRIPT:-$ROOT/scripts/baselane_statements_operator.py}"
OBIE_MATCH_SCRIPT="${BASELANE_OBIE_MATCH_SCRIPT:-$ROOT/scripts/baselane_obie_property_match.py}"
MORTGAGE_WORKFLOW_SCRIPT="${BASELANE_MORTGAGE_WORKFLOW_SCRIPT:-$ROOT/scripts/baselane_monthly_mortgage_workflow_idempotent.sh}"
MORTGAGE_WORKFLOW_REPORT="${MORTGAGE_WORKFLOW_GATE_REPORT:-$REPORT_DIR/baselane_monthly_mortgage_workflow_gate_report.json}"
MORTGAGE_WORKFLOW_TIMEOUT_SECONDS="${BASELANE_MONTHLY_MORTGAGE_WORKFLOW_TIMEOUT_SECONDS:-900}"
MORTGAGE_WORKFLOW_KILL_AFTER_SECONDS="${BASELANE_MONTHLY_MORTGAGE_WORKFLOW_KILL_AFTER_SECONDS:-30}"
MORTGAGE_WORKFLOW_TIMEOUT_ARGS=()
if command -v timeout >/dev/null 2>&1 && [ "$MORTGAGE_WORKFLOW_TIMEOUT_SECONDS" != "0" ]; then
  MORTGAGE_WORKFLOW_TIMEOUT_ARGS=(
    timeout
    --kill-after="${MORTGAGE_WORKFLOW_KILL_AFTER_SECONDS}s"
    "${MORTGAGE_WORKFLOW_TIMEOUT_SECONDS}s"
  )
fi
BASELANE_LEGACY_DIRECT_TOKENOMICS_UPDATE="${BASELANE_LEGACY_DIRECT_TOKENOMICS_UPDATE:-0}"
OBIE_TAG_SCRIPT="${BASELANE_OBIE_TAG_SCRIPT:-$ROOT/scripts/baselane_apply_obie_property_tags.js}"
PLATFORM_SPLIT_AUDIT_SCRIPT="${BASELANE_PLATFORM_SPLIT_AUDIT_SCRIPT:-$ROOT/scripts/baselane_monthly_platform_split_audit.py}"
PLATFORM_SPLIT_APPLY_SCRIPT="${BASELANE_PLATFORM_SPLIT_APPLY_SCRIPT:-$ROOT/scripts/baselane_apply_platform_subscription_splits.js}"
NATIVE_SPLIT_PLAN_SCRIPT="${BASELANE_NATIVE_SPLIT_PLAN_SCRIPT:-$ROOT/scripts/baselane_native_split_plan.py}"
NATIVE_SPLIT_APPLY_SCRIPT="${BASELANE_NATIVE_SPLIT_APPLY_SCRIPT:-${BASELANE_CONSUMABLES_SPLIT_APPLY_SCRIPT:-$ROOT/scripts/baselane_apply_native_splits.py}}"
ALIGNED_OWNER_IMPORT_SCRIPT="${BASELANE_ALIGNED_OWNER_IMPORT_SCRIPT:-$ROOT/scripts/baselane_aligned_owner_statement_import.py}"
ALIGNED_OWNER_IMPORT_REPORT="${BASELANE_ALIGNED_OWNER_IMPORT_REPORT:-$REPORT_DIR/baselane_aligned_owner_statement_import_report.json}"
ALIGNED_OWNER_IMPORT_MANIFEST_DIR="${BASELANE_ALIGNED_OWNER_IMPORT_MANIFEST_DIR:-$REPORT_DIR/aligned-owner-statement-import-manifests}"
ALIGNED_OWNER_DOWNSTREAM_VALIDATION_SCRIPT="${BASELANE_ALIGNED_OWNER_DOWNSTREAM_VALIDATION_SCRIPT:-$ROOT/scripts/validate_aligned_owner_statement_downstream.py}"
ALIGNED_OWNER_DOWNSTREAM_VALIDATION_REPORT="${BASELANE_ALIGNED_OWNER_DOWNSTREAM_VALIDATION_REPORT:-$REPORT_DIR/aligned_owner_statement_downstream_validation.json}"
ALIGNED_OWNER_SCOPE_REVIEW_SCRIPT="${BASELANE_ALIGNED_OWNER_SCOPE_REVIEW_SCRIPT:-$ROOT/scripts/review_aligned_owner_statement_cleveland_hemlane.py}"
ALIGNED_OWNER_SCOPE_REVIEW_REPORT="${BASELANE_ALIGNED_OWNER_SCOPE_REVIEW_REPORT:-$REPORT_DIR/aligned_owner_statement_cleveland_hemlane_current_review.json}"
ALIGNED_OWNER_PREAPPLY_SCOPE_REVIEW_ENABLED="${BASELANE_ALIGNED_OWNER_PREAPPLY_SCOPE_REVIEW_ENABLED:-1}"
ALIGNED_OWNER_SCOPE_PREFLIGHT_REPORT_DIR="${BASELANE_ALIGNED_OWNER_SCOPE_PREFLIGHT_REPORT_DIR:-$REPORT_DIR/aligned-owner-statement-import-scope-preflight}"
ALIGNED_OWNER_LIVE_PREFLIGHT_ENABLED="${BASELANE_ALIGNED_OWNER_LIVE_PREFLIGHT_ENABLED:-1}"
ALIGNED_OWNER_LIVE_PREFLIGHT_REPORT_DIR="${BASELANE_ALIGNED_OWNER_LIVE_PREFLIGHT_REPORT_DIR:-$REPORT_DIR/aligned-owner-statement-import-live-preflight}"
ALIGNED_OWNER_PREFLIGHT_MONTH_TIMEOUT_SECONDS="${BASELANE_ALIGNED_OWNER_PREFLIGHT_MONTH_TIMEOUT_SECONDS:-180}"
ALIGNED_OWNER_PREFLIGHT_STAGING_FALLBACK_TIMEOUT_SECONDS="${BASELANE_ALIGNED_OWNER_PREFLIGHT_STAGING_FALLBACK_TIMEOUT_SECONDS:-120}"
ALIGNED_OWNER_QUEUE_PREFLIGHT_SCRIPT="${BASELANE_ALIGNED_OWNER_QUEUE_PREFLIGHT_SCRIPT:-$ROOT/scripts/baselane_aligned_owner_statement_queue_preflight.py}"
ALIGNED_OWNER_LIVE_PREFLIGHT_SUMMARY_REPORT="${BASELANE_ALIGNED_OWNER_LIVE_PREFLIGHT_SUMMARY_REPORT:-$ALIGNED_OWNER_LIVE_PREFLIGHT_REPORT_DIR/summary.json}"
ALIGNED_OWNER_YHOME_RECONCILIATION_CSV="${BASELANE_ALIGNED_OWNER_YHOME_RECONCILIATION_CSV:-${BASELANE_MONTHLY_YHOME_TRANSITION_RECONCILIATION_CSV:-${YHOME_TRANSITION_RECONCILIATION_CSV:-$ROOT/reports/yhome_transition_reconciliation.csv}}}"
ALIGNED_OWNER_COMPLETION_GATE_SCRIPT="${BASELANE_ALIGNED_OWNER_COMPLETION_GATE_SCRIPT:-$ROOT/scripts/verify_aligned_owner_statement_completion.py}"
ALIGNED_OWNER_COMPLETION_GATE_REPORT="${BASELANE_ALIGNED_OWNER_COMPLETION_GATE_REPORT:-$REPORT_DIR/aligned_owner_statement_completion_gate.json}"
ALIGNED_OWNER_IMPORT_READINESS_SCRIPT="${BASELANE_ALIGNED_OWNER_IMPORT_READINESS_SCRIPT:-$ROOT/scripts/aligned_owner_statement_import_readiness.py}"
ALIGNED_OWNER_IMPORT_READINESS_REPORT="${BASELANE_ALIGNED_OWNER_IMPORT_READINESS_REPORT:-$REPORT_DIR/aligned_owner_statement_import_readiness.json}"
ALIGNED_OWNER_IMPORT_READINESS_MARKDOWN="${BASELANE_ALIGNED_OWNER_IMPORT_READINESS_MARKDOWN:-$REPORT_DIR/aligned_owner_statement_import_readiness.md}"
ALIGNED_OWNER_IMPORT_READINESS_TIMEOUT_SECONDS="${BASELANE_ALIGNED_OWNER_IMPORT_READINESS_TIMEOUT_SECONDS:-120}"
ALIGNED_OWNER_CF_SYNC_SCRIPT="${BASELANE_ALIGNED_OWNER_CF_SYNC_SCRIPT:-$ROOT/skills/baselane-financials/scripts/update_cf_statements.py}"
ALIGNED_OWNER_CF_SYNC_ENABLED="${BASELANE_ALIGNED_OWNER_CF_SYNC_ENABLED:-1}"
ALIGNED_OWNER_CF_SYNC_REPORT="${BASELANE_ALIGNED_OWNER_CF_SYNC_REPORT:-$REPORT_DIR/aligned_owner_statement_cf_sync_report.json}"
ALIGNED_OWNER_CF_SYNC_LOG="${BASELANE_ALIGNED_OWNER_CF_SYNC_LOG:-$REPORT_DIR/aligned_owner_statement_cf_sync.log}"
ALIGNED_OWNER_CF_SYNC_LEDGER="${BASELANE_ALIGNED_OWNER_CF_SYNC_LEDGER:-$ROOT/reports/baselane_source_transaction_index.csv}"
ALIGNED_OWNER_IMPORT_ENABLED="${BASELANE_ALIGNED_OWNER_IMPORT_ENABLED:-1}"
ALIGNED_OWNER_IMPORT_APPLY="${BASELANE_ALIGNED_OWNER_IMPORT_APPLY:-1}"
ALIGNED_OWNER_IMPORT_BACKFILL_MONTHS="${BASELANE_ALIGNED_OWNER_IMPORT_BACKFILL_MONTHS:-}"
ALIGNED_OWNER_IMPORT_BACKFILL_REPORT_DIR="${BASELANE_ALIGNED_OWNER_IMPORT_BACKFILL_REPORT_DIR:-$REPORT_DIR/aligned-owner-statement-import-backfill}"
ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE="${BASELANE_ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE:-$ROOT/config/aligned_owner_statement_backfill_queue.json}"
ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE_ACTIVE=0
ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE_STATUS="not_loaded"
ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE_ID=""
ALIGNED_OWNER_IMPORT_BACKFILL_PROPERTY_ID=""
ALIGNED_OWNER_IMPORT_READINESS_STATUS="not_started"
ALIGNED_OWNER_IMPORT_READINESS_RC=0
ALIGNED_OWNER_IMPORT_READINESS_READY=0
ALIGNED_OWNER_IMPORT_READINESS_PRIMARY_BLOCKER=""
ALIGNED_OWNER_IMPORT_CURRENT_PROPERTY_ID="${BASELANE_ALIGNED_OWNER_IMPORT_CURRENT_PROPERTY_ID:-}"
ALIGNED_OWNER_IMPORT_BACKFILL_MONTHS_NORMALIZED=""
if [ -z "$ALIGNED_OWNER_IMPORT_BACKFILL_MONTHS" ] && [ -f "$ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE" ]; then
  IFS=$'\t' read -r \
    ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE_STATUS \
    ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE_ID \
    queued_backfill_months \
    ALIGNED_OWNER_IMPORT_BACKFILL_PROPERTY_ID < <("$PY" - "$ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE" <<'PY'
import json
import sys

path = sys.argv[1]
try:
    data = json.load(open(path, encoding="utf-8"))
except Exception:
    print("unreadable\t\t")
    raise SystemExit

status = str(data.get("status") or "").strip().lower()
queue_id = str(data.get("queue_id") or "").strip()
expected = data.get("expected") if isinstance(data.get("expected"), dict) else {}
property_id = str(expected.get("baselane_property_id") or "").strip()
months = data.get("months") or []
if not isinstance(months, list):
    months = []
months_text = " ".join(str(month).strip() for month in months if str(month).strip())
print(f"{status or 'unknown'}\t{queue_id}\t{months_text}\t{property_id}")
PY
)
  if [ "$ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE_STATUS" = "queued" ] && [ -n "${queued_backfill_months:-}" ]; then
    ALIGNED_OWNER_IMPORT_BACKFILL_MONTHS="$queued_backfill_months"
    ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE_ACTIVE=1
  fi
  if [ "$ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE_ACTIVE" = "1" ] \
    && [ -z "$ALIGNED_OWNER_IMPORT_CURRENT_PROPERTY_ID" ] \
    && [ -n "$ALIGNED_OWNER_IMPORT_BACKFILL_PROPERTY_ID" ]; then
    ALIGNED_OWNER_IMPORT_CURRENT_PROPERTY_ID="$ALIGNED_OWNER_IMPORT_BACKFILL_PROPERTY_ID"
  fi
fi

YEAR="${BASELANE_MONTHLY_GATE_YEAR:-$(date +%Y)}"
MONTH="${BASELANE_MONTHLY_GATE_MONTH:-$(date +%m)}"
if [ "$MONTH" = "01" ]; then
  PREV_MONTH=12
  PREV_YEAR=$((YEAR - 1))
else
  PREV_MONTH=$((10#$MONTH - 1))
  PREV_YEAR=$YEAR
fi
DEFAULT_TARGET_YEAR="$PREV_YEAR"
DEFAULT_TARGET_MONTH="$PREV_MONTH"
if [[ "${RUN_MONTH:-}" =~ ^([0-9]{4})-([0-9]{2})$ ]]; then
  RUN_MONTH_YEAR="${BASH_REMATCH[1]}"
  RUN_MONTH_MONTH="$((10#${BASH_REMATCH[2]}))"
  if [ "$RUN_MONTH_MONTH" -ge 1 ] && [ "$RUN_MONTH_MONTH" -le 12 ]; then
    DEFAULT_TARGET_YEAR="$RUN_MONTH_YEAR"
    DEFAULT_TARGET_MONTH="$RUN_MONTH_MONTH"
  fi
fi
TARGET_YEAR="${BASELANE_MONTHLY_TARGET_YEAR:-$DEFAULT_TARGET_YEAR}"
TARGET_MONTH="${BASELANE_MONTHLY_TARGET_MONTH:-$DEFAULT_TARGET_MONTH}"
STAMP="${BASELANE_MONTHLY_TARGET_STAMP:-${TARGET_YEAR}-${TARGET_MONTH}}"
ALLOW_CURRENT_MONTH_TRANSACTION_EXPORT_CLOSE="${BASELANE_MONTHLY_ALLOW_CURRENT_MONTH_TRANSACTION_EXPORT_CLOSE:-0}"
CURRENT_LOCAL_DATE="${CURRENT_LOCAL_DATE:-$(date +%F)}"
CURRENT_LOCAL_MONTH="${CURRENT_LOCAL_DATE:0:7}"
printf -v TARGET_RUN_MONTH '%04d-%02d' "$TARGET_YEAR" "$TARGET_MONTH"
CURRENT_MONTH_TRANSACTION_EXPORT_CLOSE=0
if [ "$ALLOW_CURRENT_MONTH_TRANSACTION_EXPORT_CLOSE" = "1" ] \
  && [ "$TARGET_RUN_MONTH" = "$CURRENT_LOCAL_MONTH" ]; then
  CURRENT_MONTH_TRANSACTION_EXPORT_CLOSE=1
fi

cdp_reachable() {
  command -v curl >/dev/null 2>&1 && timeout 4 curl -fsS -H 'Host: localhost' "$1" >/dev/null 2>&1
}

cdp_has_baselane_tab() {
  local version_url="$1"
  local base_url="${version_url%/json/version}"
  command -v curl >/dev/null 2>&1 || return 1
  timeout 4 curl -fsS -H 'Host: localhost' "$base_url/json/list" 2>/dev/null | "$PY" -c 'import json, sys
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
  timeout 4 curl -fsS -H 'Host: localhost' "$base_url/json/list" 2>/dev/null | "$PY" -c 'import json, sys
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
    endpoint_host="${endpoint%:*}"
    endpoint_port="${endpoint##*:}"
  fi
  export BASELANE_CDP_URL="$base_url"
  export BASELANE_CDP_VERSION_URL="$version_url"
  export BASELANE_CDP_HOST="$endpoint_host"
  export BASELANE_CDP_PORT="$endpoint_port"
}

configure_baselane_monthly_cdp() {
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
    "http://127.0.0.1:19222/json/version"
    "http://127.0.0.1:9222/json/version"
    "http://${cyber_ts_ip}:19222/json/version"
    "http://${cyber_ts_ip}:9222/json/version"
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

if [ -z "${REAL_ESTATE_ROOT:-}" ]; then
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
configure_baselane_monthly_cdp
STATEMENTS_DOWNLOAD_DIR="${BASELANE_STATEMENTS_DOWNLOAD_DIR:-$ROOT/baselane-statements}"
if [ -z "${BASELANE_STATEMENTS_DOWNLOAD_DIR:-}" ] && curl -fsS "${BASELANE_CDP_VERSION_URL:-http://127.0.0.1:19222/json/version}" 2>/dev/null | grep -qi 'Windows NT'; then
  WINDOWS_DOWNLOAD_ROOT="/mnt/c/Users/${WINDOWS_USER:-digit}/Downloads"
  if [ -d "$WINDOWS_DOWNLOAD_ROOT" ]; then
    STATEMENTS_DOWNLOAD_DIR="$WINDOWS_DOWNLOAD_ROOT/baselane-statements"
  fi
fi
export BASELANE_STATEMENTS_DOWNLOAD_DIR="$STATEMENTS_DOWNLOAD_DIR"
PERSONAL_STATEMENTS_ROOT="${BASELANE_PERSONAL_STATEMENTS_ROOT:-$ROOT/pdf-extracts/personal/07 - P&L & Owner Statements/Bank Statements}"
HOLDINGS_STATEMENTS_ROOT="${BASELANE_HOLDINGS_STATEMENTS_ROOT:-$ROOT/pdf-extracts/business-holdings/07 - P&L & Owner Statements/Bank Statements}"
MORTGAGE_WORKFLOW_RC=0
MORTGAGE_WORKFLOW_STATUS="not_started"
MORTGAGE_WORKFLOW_SKIPPED_REASON=""
STATEMENT_GATE_REPORTED_STATUS=""
STATEMENT_GATE_REPORTED_REASON=""
STATEMENT_GATE_REPORTED_ACTION=""
STATEMENT_GATE_REPORTED_RC=0
ALIGNED_OWNER_IMPORT_RC=0
ALIGNED_OWNER_IMPORT_STATUS="not_started"
ALIGNED_OWNER_IMPORT_CREATED_COUNT=0
ALIGNED_OWNER_IMPORT_TO_CREATE_COUNT=0
ALIGNED_OWNER_IMPORT_SETTLEMENT_CANDIDATE_COUNT=0
ALIGNED_OWNER_DOWNSTREAM_VALIDATION_RC=0
ALIGNED_OWNER_DOWNSTREAM_VALIDATION_STATUS="not_started"
ALIGNED_OWNER_DOWNSTREAM_VALIDATION_FOUND_KEY_COUNT=0
ALIGNED_OWNER_DOWNSTREAM_VALIDATION_MISSING_KEY_COUNT=0
ALIGNED_OWNER_SCOPE_REVIEW_RC=0
ALIGNED_OWNER_SCOPE_REVIEW_STATUS="not_started"
ALIGNED_OWNER_SCOPE_REVIEW_SCOPE_CANDIDATE_COUNT=0
ALIGNED_OWNER_SCOPE_REVIEW_COMPLETION_STATE=""
ALIGNED_OWNER_COMPLETION_GATE_RC=0
ALIGNED_OWNER_COMPLETION_GATE_STATUS="not_started"
ALIGNED_OWNER_COMPLETION_GATE_REVIEW_REASON_COUNT=0
ALIGNED_OWNER_CF_SYNC_RC=0
ALIGNED_OWNER_CF_SYNC_STATUS="not_started"
ALIGNED_OWNER_CF_SYNC_MONTH_COUNT=0
DISK_PREFLIGHT_STATUS="not_started"
DISK_PREFLIGHT_RC=0

mkdir -p "$(dirname "$STATE_FILE")" "$REPORT_DIR"
LAST=""
if [ -f "$STATE_FILE" ]; then
  LAST="$(cat "$STATE_FILE" 2>/dev/null || true)"
fi

write_statement_gate_report() {
  local status="$1"
  local reason="$2"
  local action="$3"
  local monthly_rc="${4:-0}"
  STATEMENT_GATE_REPORTED_STATUS="$status"
  STATEMENT_GATE_REPORTED_REASON="$reason"
  STATEMENT_GATE_REPORTED_ACTION="$action"
  STATEMENT_GATE_REPORTED_RC="$monthly_rc"
  BASELANE_STATEMENT_GATE_STATUS="$status" \
  BASELANE_STATEMENT_GATE_REASON="$reason" \
  BASELANE_STATEMENT_GATE_ACTION="$action" \
  BASELANE_STATEMENT_GATE_MONTHLY_RC="$monthly_rc" \
  BASELANE_STATEMENT_GATE_STAMP="$STAMP" \
  BASELANE_STATEMENT_GATE_TARGET_YEAR="$TARGET_YEAR" \
  BASELANE_STATEMENT_GATE_TARGET_MONTH="$TARGET_MONTH" \
  BASELANE_STATEMENT_GATE_STATE_FILE="$STATE_FILE" \
  BASELANE_STATEMENT_GATE_OPERATOR_REPORT="$STATEMENTS_OPERATOR_REPORT" \
  BASELANE_STATEMENT_GATE_DOWNLOAD_REPORT="$STATEMENTS_DOWNLOAD_REPORT" \
  BASELANE_STATEMENT_GATE_DISK_PREFLIGHT_STATUS="$DISK_PREFLIGHT_STATUS" \
  BASELANE_STATEMENT_GATE_DISK_PREFLIGHT_RC="$DISK_PREFLIGHT_RC" \
  BASELANE_STATEMENT_GATE_DISK_PREFLIGHT_REPORT="$DISK_PREFLIGHT_REPORT_FILE" \
  BASELANE_STATEMENT_GATE_DISK_PREFLIGHT_MIN_FREE_MIB="$BASELANE_DISK_PREFLIGHT_MIN_FREE_MIB" \
  BASELANE_STATEMENT_GATE_MORTGAGE_WORKFLOW_REPORT="$MORTGAGE_WORKFLOW_REPORT" \
  BASELANE_STATEMENT_GATE_AUTH_RECOVERY_REPORT="$AUTH_RECOVERY_REPORT" \
  BASELANE_STATEMENT_GATE_AUTH_RECOVERY_ENABLED="$AUTH_RECOVERY_ENABLED" \
  BASELANE_STATEMENT_GATE_MORTGAGE_WORKFLOW_RC="$MORTGAGE_WORKFLOW_RC" \
  BASELANE_STATEMENT_GATE_MORTGAGE_WORKFLOW_STATUS="$MORTGAGE_WORKFLOW_STATUS" \
  BASELANE_STATEMENT_GATE_MORTGAGE_WORKFLOW_SKIPPED_REASON="$MORTGAGE_WORKFLOW_SKIPPED_REASON" \
  BASELANE_STATEMENT_GATE_MORTGAGE_WORKFLOW_TIMEOUT_SECONDS="$MORTGAGE_WORKFLOW_TIMEOUT_SECONDS" \
  BASELANE_STATEMENT_GATE_MORTGAGE_WORKFLOW_KILL_AFTER_SECONDS="$MORTGAGE_WORKFLOW_KILL_AFTER_SECONDS" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_REPORT="$ALIGNED_OWNER_IMPORT_REPORT" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_STATUS="$ALIGNED_OWNER_IMPORT_STATUS" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_RC="$ALIGNED_OWNER_IMPORT_RC" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_CREATED_COUNT="$ALIGNED_OWNER_IMPORT_CREATED_COUNT" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_TO_CREATE_COUNT="$ALIGNED_OWNER_IMPORT_TO_CREATE_COUNT" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_SETTLEMENT_CANDIDATE_COUNT="$ALIGNED_OWNER_IMPORT_SETTLEMENT_CANDIDATE_COUNT" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_DOWNSTREAM_VALIDATION_REPORT="$ALIGNED_OWNER_DOWNSTREAM_VALIDATION_REPORT" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_DOWNSTREAM_VALIDATION_STATUS="$ALIGNED_OWNER_DOWNSTREAM_VALIDATION_STATUS" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_DOWNSTREAM_VALIDATION_RC="$ALIGNED_OWNER_DOWNSTREAM_VALIDATION_RC" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_DOWNSTREAM_VALIDATION_FOUND_KEY_COUNT="$ALIGNED_OWNER_DOWNSTREAM_VALIDATION_FOUND_KEY_COUNT" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_DOWNSTREAM_VALIDATION_MISSING_KEY_COUNT="$ALIGNED_OWNER_DOWNSTREAM_VALIDATION_MISSING_KEY_COUNT" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_SCOPE_REVIEW_REPORT="$ALIGNED_OWNER_SCOPE_REVIEW_REPORT" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_SCOPE_REVIEW_STATUS="$ALIGNED_OWNER_SCOPE_REVIEW_STATUS" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_SCOPE_REVIEW_RC="$ALIGNED_OWNER_SCOPE_REVIEW_RC" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_SCOPE_REVIEW_SCOPE_CANDIDATE_COUNT="$ALIGNED_OWNER_SCOPE_REVIEW_SCOPE_CANDIDATE_COUNT" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_SCOPE_REVIEW_COMPLETION_STATE="$ALIGNED_OWNER_SCOPE_REVIEW_COMPLETION_STATE" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_COMPLETION_GATE_REPORT="$ALIGNED_OWNER_COMPLETION_GATE_REPORT" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_COMPLETION_GATE_STATUS="$ALIGNED_OWNER_COMPLETION_GATE_STATUS" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_COMPLETION_GATE_RC="$ALIGNED_OWNER_COMPLETION_GATE_RC" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_COMPLETION_GATE_REVIEW_REASON_COUNT="$ALIGNED_OWNER_COMPLETION_GATE_REVIEW_REASON_COUNT" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_READINESS_REPORT="$ALIGNED_OWNER_IMPORT_READINESS_REPORT" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_READINESS_STATUS="$ALIGNED_OWNER_IMPORT_READINESS_STATUS" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_READINESS_RC="$ALIGNED_OWNER_IMPORT_READINESS_RC" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_READINESS_READY="$ALIGNED_OWNER_IMPORT_READINESS_READY" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_READINESS_PRIMARY_BLOCKER="$ALIGNED_OWNER_IMPORT_READINESS_PRIMARY_BLOCKER" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_CF_SYNC_REPORT="$ALIGNED_OWNER_CF_SYNC_REPORT" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_CF_SYNC_STATUS="$ALIGNED_OWNER_CF_SYNC_STATUS" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_CF_SYNC_RC="$ALIGNED_OWNER_CF_SYNC_RC" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_CF_SYNC_MONTH_COUNT="$ALIGNED_OWNER_CF_SYNC_MONTH_COUNT" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE="$ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE_ACTIVE="$ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE_ACTIVE" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE_STATUS="$ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE_STATUS" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE_ID="$ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE_ID" \
  BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_CURRENT_PROPERTY_ID="$ALIGNED_OWNER_IMPORT_CURRENT_PROPERTY_ID" \
  BASELANE_STATEMENT_GATE_MIN_CAPTURED="$STATEMENTS_MIN_CAPTURED" \
  BASELANE_STATEMENT_GATE_CURRENT_MONTH_TRANSACTION_EXPORT_CLOSE="$CURRENT_MONTH_TRANSACTION_EXPORT_CLOSE" \
  BASELANE_STATEMENT_GATE_CURRENT_LOCAL_DATE="$CURRENT_LOCAL_DATE" \
  BASELANE_STATEMENT_GATE_REPORT="$STATEMENTS_IDEMPOTENT_REPORT" \
  "$PY" - <<'PY' || true
import json
import os
from datetime import datetime, timezone
from pathlib import Path

def read_json(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}

operator_report_path = os.environ["BASELANE_STATEMENT_GATE_OPERATOR_REPORT"]
download_report_path = os.environ["BASELANE_STATEMENT_GATE_DOWNLOAD_REPORT"]
mortgage_report_path = os.environ["BASELANE_STATEMENT_GATE_MORTGAGE_WORKFLOW_REPORT"]
auth_recovery_report_path = os.environ["BASELANE_STATEMENT_GATE_AUTH_RECOVERY_REPORT"]
aligned_owner_import_report_path = os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_REPORT") or ""
aligned_owner_downstream_validation_report_path = os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_DOWNSTREAM_VALIDATION_REPORT") or ""
aligned_owner_scope_review_report_path = os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_SCOPE_REVIEW_REPORT") or ""
aligned_owner_completion_gate_report_path = os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_COMPLETION_GATE_REPORT") or ""
aligned_owner_import_readiness_report_path = os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_READINESS_REPORT") or ""
aligned_owner_cf_sync_report_path = os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_CF_SYNC_REPORT") or ""
operator_report = read_json(operator_report_path)
download_report = read_json(download_report_path)
mortgage_report = read_json(mortgage_report_path)
auth_recovery_report = read_json(auth_recovery_report_path)
auth_recovery_enabled = os.environ.get("BASELANE_STATEMENT_GATE_AUTH_RECOVERY_ENABLED") == "1"
current_auth_recovery_report = auth_recovery_report if auth_recovery_enabled else {}
aligned_owner_import_report = read_json(aligned_owner_import_report_path)
aligned_owner_downstream_validation_report = read_json(aligned_owner_downstream_validation_report_path)
aligned_owner_scope_review_report = read_json(aligned_owner_scope_review_report_path)
aligned_owner_completion_gate_report = read_json(aligned_owner_completion_gate_report_path)
aligned_owner_import_readiness_report = read_json(aligned_owner_import_readiness_report_path)
aligned_owner_cf_sync_report = read_json(aligned_owner_cf_sync_report_path)
aligned_owner_cf_workbook = aligned_owner_downstream_validation_report.get("cash_flow_workbook") or {}
aligned_owner_cf_candidates = aligned_owner_cf_workbook.get("candidates") or []
aligned_owner_cf_schema_labels = aligned_owner_cf_workbook.get("schema_labels") or []
aligned_owner_cf_selected_schema_priority = aligned_owner_cf_workbook.get("selected_schema_priority") or []
aligned_owner_cf_selected_schema = (
    aligned_owner_cf_selected_schema_priority[1]
    if len(aligned_owner_cf_selected_schema_priority) > 1
    else None
)
aligned_owner_cf_duplicate_candidate_count = max(len(aligned_owner_cf_candidates) - 1, 0)
aligned_owner_cf_mixed_template_candidate_count = int(
    aligned_owner_cf_workbook.get("mixed_template_candidate_count") or 0
)
aligned_owner_ledger_label_presence = aligned_owner_downstream_validation_report.get("ledger_label_presence") or {}
aligned_owner_scope_import_coverage = aligned_owner_scope_review_report.get("import_coverage") or {}
aligned_owner_import_readiness_preflight = aligned_owner_import_readiness_report.get("preflight") or {}
aligned_owner_import_readiness_scope = aligned_owner_import_readiness_report.get("scope_review") or {}
aligned_owner_import_label_guard = aligned_owner_import_readiness_report.get("import_label_guard") or {}
download_error = str(download_report.get("error") or "")
statements_assumed_verified = os.environ.get("BASELANE_MONTHLY_ASSUME_STATEMENTS_VERIFIED") == "1"
current_month_transaction_export_close = (
    os.environ.get("BASELANE_STATEMENT_GATE_CURRENT_MONTH_TRANSACTION_EXPORT_CLOSE") == "1"
)
download_attempted = not statements_assumed_verified and not current_month_transaction_export_close
gate_reason = os.environ.get("BASELANE_STATEMENT_GATE_REASON") or None
graphql_results = download_report.get("graphql_click_results") or []
graphql_error_count = 0
first_graphql_error = {}
for result in graphql_results:
    errors = result.get("errors") if isinstance(result, dict) else None
    if not isinstance(errors, list) or not errors:
        continue
    graphql_error_count += len(errors)
    if not first_graphql_error:
        first_graphql_error = {
            "operation_name": result.get("operation_name"),
            "message": str((errors[0] or {}).get("message") or "")[:200],
            "code": (errors[0] or {}).get("code"),
            "service_name": (errors[0] or {}).get("service_name"),
        }
download_error_class = None
if gate_reason == "auth-required" or "AUTH_REQUIRED" in download_error or "login form submission failed" in download_error:
    download_error_class = "auth-required"
elif "unitAPINonSensitiveToken" in download_error and "404" in download_error:
    download_error_class = "unit-token-404"
elif "no statement download buttons discovered" in download_error:
    download_error_class = "no-statement-buttons"
elif "CDP command timed out" in download_error:
    download_error_class = "cdp-timeout"
elif download_error:
    download_error_class = "download-error"
target_year = int(os.environ["BASELANE_STATEMENT_GATE_TARGET_YEAR"])
target_month = int(os.environ["BASELANE_STATEMENT_GATE_TARGET_MONTH"])
run_month = f"{target_year:04d}-{target_month:02d}"
gate_refresh_command = (
    f"RUN_MONTH={run_month} BASELANE_MONTHLY_STATEMENTS_GATE_ONLY=1 "
    "bash scripts/baselane_monthly_statements_idempotent.sh"
)
retry_command = f"RUN_MONTH={run_month} bash scripts/baselane_monthly_statements_idempotent.sh"
auth_recovery_hint = None
if gate_reason == "auth-required" or download_error_class == "auth-required":
    recovery_performed = bool(current_auth_recovery_report.get("login_recovery_performed"))
    recovery_authenticated = current_auth_recovery_report.get("status") == "ok"
    if recovery_authenticated:
        auth_recovery_hint = (
            "Automation found an authenticated Baselane tab after recovery; "
            "rerun the statement capture command."
        )
        next_action = f"{auth_recovery_hint} Command: {retry_command}"
    elif recovery_performed:
        auth_recovery_hint = (
            "Automation already hard-refreshed/reopened the Baselane statements tab; "
            "finish login in the visible tab, then run the gate refresh command."
        )
        next_action = f"{auth_recovery_hint} Command: {gate_refresh_command}"
    else:
        auth_recovery_hint = (
            "Hard refresh Baselane login, or close/open a new Baselane tab if stuck; "
            "finish login, then run the gate refresh command."
        )
        next_action = f"{auth_recovery_hint} Command: {gate_refresh_command}"
elif gate_reason == "no-statement-buttons" or download_error_class == "no-statement-buttons":
    auth_recovery_hint = "Baselane has no target-month statement download buttons yet."
    next_action = f"{auth_recovery_hint} Retry after statements post. Command: {retry_command}"
elif gate_reason == "disk-space-preflight":
    next_action = "Free local Dropbox/Windows disk space, then rerun monthly statement capture."
elif os.environ["BASELANE_STATEMENT_GATE_STATUS"] == "ok":
    if current_month_transaction_export_close:
        next_action = (
            "No current-month statement action required; the close uses the live "
            "transaction export until the final bank statement posts."
        )
    else:
        next_action = "No statement action required; current target-month capture is verified."
else:
    next_action = f"Run statement capture retry: {retry_command}"
report = {
    "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "job": "baselane-monthly-statements-idempotent",
    "status": os.environ["BASELANE_STATEMENT_GATE_STATUS"],
    "dry_run": os.environ.get("DRY_RUN") == "1",
    "reason": gate_reason,
    "action": os.environ.get("BASELANE_STATEMENT_GATE_ACTION") or None,
    "monthly_script_return_code": int(os.environ.get("BASELANE_STATEMENT_GATE_MONTHLY_RC") or 0),
    "run_month": run_month,
    "stamp": os.environ["BASELANE_STATEMENT_GATE_STAMP"],
    "target_year": target_year,
    "target_month": target_month,
    "current_local_date": os.environ.get("BASELANE_STATEMENT_GATE_CURRENT_LOCAL_DATE"),
    "current_month_transaction_export_close": current_month_transaction_export_close,
    "close_source_basis": (
        "live_transaction_export"
        if current_month_transaction_export_close
        else "bank_statements"
    ),
    "next_action": next_action,
    "gate_refresh_command": gate_refresh_command,
    "retry_command": retry_command,
    "auth_recovery_hint": auth_recovery_hint,
    "auth_recovery_report": auth_recovery_report_path,
    "auth_recovery_enabled": auth_recovery_enabled,
    "auth_recovery_status": current_auth_recovery_report.get("status"),
    "auth_recovery_attempted": bool(current_auth_recovery_report.get("login_recovery_performed")),
    "auth_recovery_attempt_count": current_auth_recovery_report.get("login_recovery_attempt_count"),
    "auth_recovery_opened_statements": current_auth_recovery_report.get("login_recovery_opened_statements"),
    "auth_recovery_manual_auth_required": current_auth_recovery_report.get("manual_auth_required"),
    "state_file": os.environ["BASELANE_STATEMENT_GATE_STATE_FILE"],
    "disk_space_preflight_status": os.environ.get("BASELANE_STATEMENT_GATE_DISK_PREFLIGHT_STATUS"),
    "disk_space_preflight_rc": int(os.environ.get("BASELANE_STATEMENT_GATE_DISK_PREFLIGHT_RC") or 0),
    "disk_space_preflight_report": os.environ.get("BASELANE_STATEMENT_GATE_DISK_PREFLIGHT_REPORT"),
    "disk_space_preflight_min_free_mib": int(os.environ.get("BASELANE_STATEMENT_GATE_DISK_PREFLIGHT_MIN_FREE_MIB") or 0),
    "operator_report": operator_report_path,
    "operator_status": operator_report.get("status"),
    "operator_ok_state": operator_report.get("ok_state"),
    "operator_issue_count": operator_report.get("issue_count"),
    "operator_destination_rule_validation_enabled": operator_report.get("destination_rule_validation_enabled"),
    "operator_destination_rule_issue_count": operator_report.get("destination_rule_issue_count"),
    "operator_destination_rule_issues": operator_report.get("destination_rule_issues") or [],
    "captured_unique_count": operator_report.get("captured_unique_count"),
    "min_captured_required": int(os.environ.get("BASELANE_STATEMENT_GATE_MIN_CAPTURED") or 0),
    "download_report": download_report_path,
    "download_attempted": download_attempted,
    "download_report_not_used": statements_assumed_verified or current_month_transaction_export_close,
    "download_ok": None if not download_attempted else download_report.get("ok"),
    "download_new_files_count": None if not download_attempted else download_report.get("new_files_count"),
    "download_total_buttons": None if not download_attempted else download_report.get("total_buttons"),
    "download_clicked_buttons": None if not download_attempted else download_report.get("clicked_buttons"),
    "download_error": None if not download_attempted else (download_error or None),
    "download_error_class": None if not download_attempted else download_error_class,
    "download_graphql_error_count": 0 if not download_attempted else graphql_error_count,
    "download_first_graphql_error": None if not download_attempted else (first_graphql_error or None),
    "mortgage_workflow_report": mortgage_report_path,
    "mortgage_workflow_rc": int(os.environ.get("BASELANE_STATEMENT_GATE_MORTGAGE_WORKFLOW_RC") or 0),
    "mortgage_workflow_invocation_status": os.environ.get("BASELANE_STATEMENT_GATE_MORTGAGE_WORKFLOW_STATUS") or "not_started",
    "mortgage_workflow_timeout_seconds": int(os.environ.get("BASELANE_STATEMENT_GATE_MORTGAGE_WORKFLOW_TIMEOUT_SECONDS") or 0),
    "mortgage_workflow_kill_after_seconds": int(os.environ.get("BASELANE_STATEMENT_GATE_MORTGAGE_WORKFLOW_KILL_AFTER_SECONDS") or 0),
    "mortgage_workflow_skipped": (os.environ.get("BASELANE_STATEMENT_GATE_MORTGAGE_WORKFLOW_STATUS") or "") == "skipped",
    "mortgage_workflow_skipped_reason": os.environ.get("BASELANE_STATEMENT_GATE_MORTGAGE_WORKFLOW_SKIPPED_REASON") or None,
    "mortgage_workflow_status": mortgage_report.get("status"),
    "mortgage_workflow_reason": mortgage_report.get("reason"),
    "aligned_owner_import_report": aligned_owner_import_report_path or None,
    "aligned_owner_import_status": os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_STATUS") or aligned_owner_import_report.get("status"),
    "aligned_owner_import_rc": int(os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_RC") or 0),
    "aligned_owner_import_created_count": int(os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_CREATED_COUNT") or aligned_owner_import_report.get("created_count") or 0),
    "aligned_owner_import_to_create_count": int(os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_TO_CREATE_COUNT") or aligned_owner_import_report.get("to_create_count") or 0),
    "aligned_owner_import_settlement_candidate_count": int(os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_SETTLEMENT_CANDIDATE_COUNT") or aligned_owner_import_report.get("settlement_relabel_candidate_count") or 0),
    "aligned_owner_downstream_validation_report": aligned_owner_downstream_validation_report_path or None,
    "aligned_owner_downstream_validation_status": os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_DOWNSTREAM_VALIDATION_STATUS") or aligned_owner_downstream_validation_report.get("status"),
    "aligned_owner_downstream_validation_rc": int(os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_DOWNSTREAM_VALIDATION_RC") or 0),
    "aligned_owner_downstream_validation_found_key_count": int(os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_DOWNSTREAM_VALIDATION_FOUND_KEY_COUNT") or aligned_owner_downstream_validation_report.get("ledger_presence", {}).get("found_key_count") or 0),
    "aligned_owner_downstream_validation_missing_key_count": int(os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_DOWNSTREAM_VALIDATION_MISSING_KEY_COUNT") or aligned_owner_downstream_validation_report.get("ledger_presence", {}).get("missing_key_count") or 0),
    "aligned_owner_downstream_validation_expected_tag_count": int(aligned_owner_ledger_label_presence.get("expected_tag_count") or 0),
    "aligned_owner_downstream_validation_checked_label_key_count": int(aligned_owner_ledger_label_presence.get("checked_key_count") or 0),
    "aligned_owner_downstream_validation_label_mismatch_count": int(aligned_owner_ledger_label_presence.get("mismatch_count") or 0),
    "aligned_owner_scope_review_report": aligned_owner_scope_review_report_path or None,
    "aligned_owner_scope_review_status": os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_SCOPE_REVIEW_STATUS") or ("ok" if aligned_owner_scope_review_report else "not_started"),
    "aligned_owner_scope_review_rc": int(os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_SCOPE_REVIEW_RC") or 0),
    "aligned_owner_scope_review_scope_candidate_count": int(os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_SCOPE_REVIEW_SCOPE_CANDIDATE_COUNT") or len(aligned_owner_scope_review_report.get("scope_candidates") or [])),
    "aligned_owner_scope_review_completion_state": os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_SCOPE_REVIEW_COMPLETION_STATE") or aligned_owner_scope_review_report.get("completion_state"),
    "aligned_owner_scope_review_zero_row_property_count": aligned_owner_scope_import_coverage.get("zero_row_property_count"),
    "aligned_owner_scope_review_nonzero_dry_run_property_count": aligned_owner_scope_import_coverage.get("nonzero_dry_run_property_count"),
    "aligned_owner_scope_review_queued_nonzero_property_count": aligned_owner_scope_import_coverage.get("queued_nonzero_property_count"),
    "aligned_owner_scope_review_unqueued_nonzero_property_count": aligned_owner_scope_import_coverage.get("unqueued_nonzero_property_count"),
    "aligned_owner_scope_review_cash_flow_duplicate_template_warning_count": aligned_owner_scope_review_report.get("cash_flow_duplicate_template_warning_count"),
    "aligned_owner_scope_review_cash_flow_blocking_duplicate_template_warning_count": aligned_owner_scope_review_report.get("cash_flow_blocking_duplicate_template_warning_count"),
    "aligned_owner_completion_gate_report": aligned_owner_completion_gate_report_path or None,
    "aligned_owner_completion_gate_status": os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_COMPLETION_GATE_STATUS") or aligned_owner_completion_gate_report.get("status"),
    "aligned_owner_completion_gate_rc": int(os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_COMPLETION_GATE_RC") or 0),
    "aligned_owner_completion_gate_review_reason_count": int(os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_COMPLETION_GATE_REVIEW_REASON_COUNT") or len(aligned_owner_completion_gate_report.get("review_reasons") or [])),
    "aligned_owner_completion_gate_review_reasons": aligned_owner_completion_gate_report.get("review_reasons") or [],
    "aligned_owner_completion_gate_failed_checks": aligned_owner_completion_gate_report.get("failed_checks") or aligned_owner_completion_gate_report.get("review_reasons") or [],
    "aligned_owner_completion_gate_primary_blocker": aligned_owner_completion_gate_report.get("primary_blocker"),
    "aligned_owner_completion_gate_queue_status": aligned_owner_completion_gate_report.get("queue_status"),
    "aligned_owner_completion_gate_ledger_found_key_count": aligned_owner_completion_gate_report.get("ledger_found_key_count"),
    "aligned_owner_completion_gate_ledger_missing_key_count": aligned_owner_completion_gate_report.get("ledger_missing_key_count"),
    "aligned_owner_completion_gate_cash_flow_selected_schema": aligned_owner_completion_gate_report.get("cash_flow_selected_schema"),
    "aligned_owner_import_readiness_report": aligned_owner_import_readiness_report_path or None,
    "aligned_owner_import_readiness_status": os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_READINESS_STATUS") or aligned_owner_import_readiness_report.get("status"),
    "aligned_owner_import_readiness_rc": int(os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_READINESS_RC") or 0),
    "aligned_owner_import_readiness_ready_for_cron_live_import": (
        os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_READINESS_READY") == "1"
        or aligned_owner_import_readiness_report.get("ready_for_cron_live_import") is True
    ),
    "aligned_owner_import_readiness_primary_blocker": os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_READINESS_PRIMARY_BLOCKER") or aligned_owner_import_readiness_report.get("primary_blocker"),
    "aligned_owner_import_readiness_auth_ok": (aligned_owner_import_readiness_report.get("auth") or {}).get("ok"),
    "aligned_owner_import_readiness_scope_ready": aligned_owner_import_readiness_scope.get("ready_for_cron_owned_live_import"),
    "aligned_owner_import_readiness_scope_duplicate_template_warning_count": aligned_owner_import_readiness_scope.get("cash_flow_duplicate_template_warning_count"),
    "aligned_owner_import_readiness_scope_blocking_duplicate_template_warning_count": aligned_owner_import_readiness_scope.get("cash_flow_blocking_duplicate_template_warning_count"),
    "aligned_owner_import_readiness_preflight_ok": aligned_owner_import_readiness_preflight.get("ok"),
    "aligned_owner_import_readiness_duplicate_preflight_complete": aligned_owner_import_readiness_preflight.get("duplicate_check_complete"),
    "aligned_owner_import_readiness_duplicate_check_trusted_zero": aligned_owner_import_readiness_preflight.get("duplicate_check_trusted_zero"),
    "aligned_owner_import_readiness_duplicate_or_existing_key_count": aligned_owner_import_readiness_preflight.get("duplicate_or_existing_key_count"),
    "aligned_owner_import_readiness_preflight_query_error_month_count": aligned_owner_import_readiness_preflight.get("query_error_month_count"),
    "aligned_owner_import_readiness_preflight_auth_error_month_count": aligned_owner_import_readiness_preflight.get("auth_error_month_count"),
    "aligned_owner_import_readiness_preflight_pre_fallback_query_error_month_count": aligned_owner_import_readiness_preflight.get("pre_fallback_query_error_month_count"),
    "aligned_owner_import_readiness_preflight_pre_fallback_auth_error_month_count": aligned_owner_import_readiness_preflight.get("pre_fallback_auth_error_month_count"),
    "aligned_owner_import_readiness_preflight_timed_out_month_count": aligned_owner_import_readiness_preflight.get("timed_out_month_count"),
    "aligned_owner_import_readiness_preflight_missing_report_month_count": aligned_owner_import_readiness_preflight.get("missing_report_month_count"),
    "aligned_owner_import_readiness_label_guard_ok": aligned_owner_import_label_guard.get("ok"),
    "aligned_owner_import_readiness_label_guard_status": aligned_owner_import_label_guard.get("status"),
    "aligned_owner_import_readiness_label_guard_report_count": aligned_owner_import_label_guard.get("report_count"),
    "aligned_owner_import_readiness_label_guard_expected_month_count": aligned_owner_import_label_guard.get("expected_month_count"),
    "aligned_owner_import_readiness_label_guard_disallowed_count": aligned_owner_import_label_guard.get("disallowed_rich_category_count"),
    "aligned_owner_import_readiness_label_guard_expected_disallowed_count": aligned_owner_import_label_guard.get("expected_disallowed_rich_category_count"),
    "aligned_owner_import_readiness_label_guard_tag_mismatch_count": aligned_owner_import_label_guard.get("tag_mismatch_count"),
    "aligned_owner_cf_sync_report": aligned_owner_cf_sync_report_path or None,
    "aligned_owner_cf_sync_status": os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_CF_SYNC_STATUS") or aligned_owner_cf_sync_report.get("status"),
    "aligned_owner_cf_sync_rc": int(os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_CF_SYNC_RC") or aligned_owner_cf_sync_report.get("return_code") or 0),
    "aligned_owner_cf_sync_month_count": int(os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_CF_SYNC_MONTH_COUNT") or aligned_owner_cf_sync_report.get("month_count") or 0),
    "aligned_owner_cf_selected_workbook": aligned_owner_cf_workbook.get("selected"),
    "aligned_owner_cf_selected_schema": aligned_owner_cf_selected_schema,
    "aligned_owner_cf_candidate_count": len(aligned_owner_cf_candidates),
    "aligned_owner_cf_candidates": aligned_owner_cf_candidates,
    "aligned_owner_cf_schema_labels": aligned_owner_cf_schema_labels,
    "aligned_owner_cf_duplicate_candidate_count": aligned_owner_cf_duplicate_candidate_count,
    "aligned_owner_cf_mixed_template_candidate_count": aligned_owner_cf_mixed_template_candidate_count,
    "aligned_owner_import_backfill_months": [m for m in (os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_BACKFILL_MONTHS") or "").split() if m],
    "aligned_owner_import_backfill_report_dir": os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_BACKFILL_REPORT_DIR") or None,
    "aligned_owner_import_backfill_created_count": int(os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_BACKFILL_CREATED_COUNT") or 0),
    "aligned_owner_import_backfill_to_create_count": int(os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_BACKFILL_TO_CREATE_COUNT") or 0),
    "aligned_owner_import_backfill_queue": os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE") or None,
    "aligned_owner_import_backfill_queue_active": os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE_ACTIVE") == "1",
    "aligned_owner_import_backfill_queue_status": os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE_STATUS") or None,
    "aligned_owner_import_backfill_queue_id": os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE_ID") or None,
    "aligned_owner_import_current_property_id": os.environ.get("BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_CURRENT_PROPERTY_ID") or None,
}
aligned_queue_pending = (
    report.get("aligned_owner_import_backfill_queue_active") is True
    and report.get("aligned_owner_import_backfill_queue_status") == "queued"
)
aligned_readiness_ready = report.get("aligned_owner_import_readiness_ready_for_cron_live_import") is True
aligned_readiness_timeout = (
    str(report.get("aligned_owner_import_readiness_status") or "") == "review_timeout"
    or int(report.get("aligned_owner_import_readiness_rc") or 0) in {124, 137}
)
aligned_completion_status = str(report.get("aligned_owner_completion_gate_status") or "")
aligned_completion_complete = aligned_completion_status in {"ok", "complete", "completed"}
aligned_downstream_ok = str(report.get("aligned_owner_downstream_validation_status") or "") == "ok"
aligned_live_duplicate_check_complete = (
    report.get("aligned_owner_import_readiness_duplicate_preflight_complete") is True
    and report.get("aligned_owner_import_readiness_duplicate_check_trusted_zero") is True
)
aligned_owner_blocks_statement_gate = (
    aligned_readiness_timeout
    or (
        aligned_queue_pending
    and not (aligned_readiness_ready and aligned_completion_complete and aligned_downstream_ok)
    )
)
if aligned_owner_blocks_statement_gate and report.get("status") == "ok":
    reported_status = report.get("status")
    reported_reason = report.get("reason")
    reported_action = report.get("action")
    blocker = (
        report.get("aligned_owner_import_readiness_primary_blocker")
        or report.get("aligned_owner_completion_gate_primary_blocker")
        or (
            "live_duplicate_preflight_not_trusted_zero"
            if not aligned_live_duplicate_check_complete
            else "queued_aligned_owner_import_not_completed"
        )
    )
    report["reported_status_before_aligned_owner_gate"] = reported_status
    report["reported_reason_before_aligned_owner_gate"] = reported_reason
    report["reported_action_before_aligned_owner_gate"] = reported_action
    report["status"] = "review"
    report["reason"] = (
        "aligned-owner-import-readiness-timeout"
        if aligned_readiness_timeout
        else "aligned-owner-import-pending"
    )
    report["action"] = (
        "retry-aligned-owner-readiness"
        if aligned_readiness_timeout
        else "review-aligned-owner-import"
    )
    report["aligned_owner_import_blocks_statement_gate"] = True
    report["aligned_owner_import_blocker"] = blocker
    report["next_action"] = (
        "Resolve queued Aligned owner-statement import readiness before treating "
        f"the monthly statement gate as complete. Blocker: {blocker}. "
        f"Open: {report.get('aligned_owner_import_readiness_report') or report.get('aligned_owner_completion_gate_report')}"
    )
else:
    report["aligned_owner_import_blocks_statement_gate"] = False
    report["aligned_owner_import_blocker"] = None
Path(os.environ["BASELANE_STATEMENT_GATE_REPORT"]).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps(report, indent=2, sort_keys=True))
PY
}

if [ -x "$ROOT/scripts/baselane_disk_space_preflight.py" ]; then
  set +e
  timeout --kill-after="${BASELANE_DISK_PREFLIGHT_KILL_AFTER_SECONDS}s" \
    "${BASELANE_DISK_PREFLIGHT_TIMEOUT_SECONDS}s" \
    "$PY" "$ROOT/scripts/baselane_disk_space_preflight.py" \
    --path "real_estate_root=$REAL_ESTATE_ROOT" \
    --path "statements_download_dir=$STATEMENTS_DOWNLOAD_DIR" \
    --min-free-mib "$BASELANE_DISK_PREFLIGHT_MIN_FREE_MIB" \
    --report "$DISK_PREFLIGHT_REPORT_FILE" >/dev/null
  DISK_PREFLIGHT_RC="$?"
  set -e
  if [ "$DISK_PREFLIGHT_RC" -eq 0 ]; then
    DISK_PREFLIGHT_STATUS="ok"
  else
    DISK_PREFLIGHT_STATUS="review"
    write_statement_gate_report "review" "disk-space-preflight" "free-disk" 0
    echo "[baselane-monthly] holding statement capture because local disk preflight is review; see $DISK_PREFLIGHT_REPORT_FILE" >&2
    exit 0
  fi
else
  DISK_PREFLIGHT_STATUS="missing"
fi

audit_statement_capture() {
  if [ ! -f "$STATEMENTS_OPERATOR_SCRIPT" ]; then
    write_statement_gate_report "review" "missing-statements-operator" "run-statements"
    return 1
  fi

  set +e
  "$PY" "$STATEMENTS_OPERATOR_SCRIPT" \
    --year "$TARGET_YEAR" \
    --month "$TARGET_MONTH" \
    --downloads "$STATEMENTS_DOWNLOAD_DIR" \
    --real-estate "$REAL_ESTATE_ROOT" \
    --personal "$PERSONAL_STATEMENTS_ROOT" \
    --holdings "$HOLDINGS_STATEMENTS_ROOT" \
    --json > "$STATEMENTS_OPERATOR_REPORT"
  operator_rc="$?"
  set -e
  if [ "$operator_rc" -ne 0 ]; then
    write_statement_gate_report "review" "operator-review-required" "run-statements"
    return 1
  fi

  set +e
  "$PY" - "$STATEMENTS_OPERATOR_REPORT" "$STATEMENTS_DOWNLOAD_REPORT" "$STATEMENTS_MIN_CAPTURED" "$TARGET_YEAR" "$TARGET_MONTH" <<'PY'
import json
import sys
from pathlib import Path

operator_path = Path(sys.argv[1])
download_path = Path(sys.argv[2])
min_captured = int(sys.argv[3])
target_year = str(sys.argv[4])
target_month_number = int(sys.argv[5])
month_names = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sept", 10: "Oct", 11: "Nov", 12: "Dec",
}
operator_report = json.loads(operator_path.read_text(encoding="utf-8"))
captured = int(operator_report.get("captured_unique_count") or 0)
if captured < min_captured:
    raise SystemExit(f"captured_unique_count {captured} below minimum {min_captured}")
if int(operator_report.get("issue_count") or 0) != 0:
    raise SystemExit("operator issue_count is not zero")
if not download_path.exists():
    raise SystemExit("download report missing")
download_report = json.loads(download_path.read_text(encoding="utf-8"))
if download_report.get("ok") is not True:
    raise SystemExit("download report not ok")
download_year = str(download_report.get("target_year") or "")
download_month = str(download_report.get("target_month") or "")
if download_year and download_year != target_year:
    raise SystemExit(f"download report target_year {download_year} != {target_year}")
expected_month_values = {str(target_month_number), month_names[target_month_number]}
if download_month and download_month not in expected_month_values:
    raise SystemExit(f"download report target_month {download_month} not in {sorted(expected_month_values)}")
PY
  validation_rc="$?"
  set -e
  if [ "$validation_rc" -ne 0 ]; then
    write_statement_gate_report "review" "statement-capture-verification-failed" "run-statements"
    return "$validation_rc"
  fi
}

run_auth_recovery_if_enabled() {
  if [ "$AUTH_RECOVERY_ENABLED" != "1" ] || [ ! -f "$AUTH_RECOVERY_SCRIPT" ]; then
    return 0
  fi
  set +e
  "$PY" "$AUTH_RECOVERY_SCRIPT" \
    --recover-login \
    --graphql-auth-smoke \
    --recovery-wait-seconds "${BASELANE_AUTH_RECOVERY_WAIT_SECONDS:-2}" \
    --report "$AUTH_RECOVERY_REPORT" >/tmp/baselane-auth-recovery.out 2>&1
  recovery_rc="$?"
  set -e
  if [ "$recovery_rc" -ne 0 ]; then
    echo "[baselane-monthly] Baselane auth recovery remains review; see $AUTH_RECOVERY_REPORT" >&2
  fi
  return 0
}

auth_recovery_status() {
  "$PY" - "$AUTH_RECOVERY_REPORT" <<'PY' || true
import json
import sys
from pathlib import Path

try:
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except Exception:
    payload = {}
print(payload.get("status") or "")
PY
}

auth_recovery_manual_auth_required() {
  "$PY" - "$AUTH_RECOVERY_REPORT" <<'PY' || true
import json
import sys
from pathlib import Path

try:
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except Exception:
    payload = {}
print("1" if payload.get("manual_auth_required") is True else "0")
PY
}

aligned_live_preflight_summary_fields() {
  "$PY" - "$1" <<'PY'
import json
import sys
from pathlib import Path

AUTH_MARKERS = (
    "x-firebase-appcheck",
    "auth_required",
    "unauthorized_access",
    "missing cookie",
    "session-expired",
    "session expired",
    "manual_auth_required",
    "auth required",
)

def walk_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from walk_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_strings(child)

try:
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except Exception:
    print("missing_report\tmissing_report\t0\t0\t0")
    raise SystemExit

review_reasons = [str(reason) for reason in (data.get("review_reasons") or [])]
reason_text = ",".join(review_reasons) or "-"
auth_error_months = data.get("auth_error_months") or []
haystack = "\n".join(walk_strings(data.get("commands") or []))
haystack += "\n" + "\n".join(walk_strings(data.get("query_error_months") or []))
haystack += "\n" + "\n".join(review_reasons)
haystack = haystack.lower()
auth_like = bool(auth_error_months) or any(
    marker in haystack for marker in AUTH_MARKERS
) or "auth_required" in reason_text.lower()

print(
    data.get("status") or "unknown",
    reason_text,
    int(data.get("skipped_existing_count_total") or 0),
    int(data.get("to_create_count_total") or 0),
    "1" if auth_like else "0",
    sep="\t",
)
PY
}

run_aligned_live_queue_preflight() {
  set +e
  "$PY" "$ALIGNED_OWNER_QUEUE_PREFLIGHT_SCRIPT" "${aligned_live_preflight_args[@]}" >/dev/null
  aligned_live_preflight_rc=$?
  set -e
}

normalize_aligned_live_preflight_reasons() {
  if [ "${aligned_live_preflight_reasons:-}" = "-" ]; then
    aligned_live_preflight_reasons=""
  fi
}

assert_no_apply_in_dry_run() {
  local label="$1"
  shift
  if [ "${DRY_RUN:-0}" != "1" ]; then
    return 0
  fi
  local arg
  for arg in "$@"; do
    if [ "$arg" = "--apply" ]; then
      echo "[baselane-monthly] Refusing dry-run $label: Aligned owner importer arguments include --apply" >&2
      write_statement_gate_report "error" "dry-run-aligned-owner-apply-refused" "review-aligned-owner-import" 2
      exit 2
    fi
  done
}

statement_download_error_class() {
  "$PY" - "$STATEMENTS_DOWNLOAD_REPORT" <<'PY' || true
import json
import sys
from pathlib import Path

try:
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except Exception:
    payload = {}
error = str(payload.get("error") or "")
if "no statement download buttons discovered" in error:
    print("no-statement-buttons")
elif "no new PDF files" in error:
    print("no-new-pdfs")
elif "unitAPINonSensitiveToken" in error and "404" in error:
    print("unit-token-404")
elif "CDP command timed out" in error:
    print("cdp-timeout")
elif "AUTH_REQUIRED" in error or "login form submission failed" in error:
    print("auth-required")
elif error:
    print("download-error")
else:
    print("")
PY
}

auth_preflight_or_report() {
  if [ ! -f "$AUTH_PREFLIGHT_SCRIPT" ]; then
    return 0
  fi
  set +e
  "$PY" "$AUTH_PREFLIGHT_SCRIPT" --report "$AUTH_PREFLIGHT_REPORT" >/tmp/baselane-auth-preflight.out 2>&1
  auth_rc="$?"
  set -e
  if [ "$auth_rc" -eq 0 ]; then
    return 0
  fi
  auth_error="$("$PY" - "$AUTH_PREFLIGHT_REPORT" <<'PY' || true
import json
import sys
from pathlib import Path

try:
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except Exception:
    payload = {}
print(str(payload.get("error") or "auth-preflight-failed"))
PY
)"
  reason="auth-preflight-failed"
  case "$auth_error" in
    AUTH_REQUIRED) reason="auth-required" ;;
    MAINTENANCE_PAGE) reason="baselane-maintenance" ;;
  esac
  if [ "$reason" = "auth-required" ] || [ "${BASELANE_AUTH_RECOVERY_CONTINUE_ON_OK:-0}" = "1" ]; then
    run_auth_recovery_if_enabled
    if [ "${BASELANE_AUTH_RECOVERY_CONTINUE_ON_OK:-0}" = "1" ] && [ "$(auth_recovery_status)" = "ok" ]; then
      echo "[baselane-monthly] Baselane auth recovery verified an authenticated tab after preflight ${reason}; continuing statement capture..."
      return 0
    fi
  fi
  write_statement_gate_report "review" "$reason" "auth-baselane" 0
  echo "[baselane-monthly] Baselane auth preflight blocked statement capture: $reason" >&2
  return 1
}

if [ "${BASELANE_MONTHLY_STATEMENTS_GATE_ONLY:-0}" = "1" ]; then
  echo "[baselane-monthly] Refreshing statement gate only for ${STAMP}..."
  if audit_statement_capture; then
    write_statement_gate_report "ok" "existing-capture-verified-gate-refresh" "refresh"
    exit 0
  fi
  if [ "$(statement_download_error_class)" = "no-statement-buttons" ]; then
    write_statement_gate_report "review" "no-statement-buttons" "wait-for-statements" 0
    exit 0
  fi
  auth_preflight_or_report || exit 0
  exit 0
fi

if [ "$CURRENT_MONTH_TRANSACTION_EXPORT_CLOSE" = "1" ]; then
  echo "[baselane-monthly] Using live transaction export for current-month close ${STAMP}"
  write_statement_gate_report "ok" "current-month-live-transaction-export-close" "transaction-export"
elif [ "${BASELANE_MONTHLY_ASSUME_STATEMENTS_VERIFIED:-0}" = "1" ]; then
  echo "[baselane-monthly] Assuming statements for ${STAMP} are verified by caller"
  write_statement_gate_report "ok" "assumed-statements-verified" "external-verified"
elif [ "$LAST" = "$STAMP" ] && audit_statement_capture; then
  echo "[baselane-monthly] Statements for ${STAMP} already captured and verified, skipping statements"
  write_statement_gate_report "ok" "already-captured-and-verified" "skip"
else
  if [ ! -x "$MONTHLY_SCRIPT" ]; then
    echo "[baselane-monthly] Missing executable monthly script: $MONTHLY_SCRIPT" >&2
    write_statement_gate_report "error" "missing-monthly-script" "run-statements" 1
    exit 1
  fi

  echo "[baselane-monthly] Running statements for ${STAMP}..."
  BASELANE_AUTH_RECOVERY_CONTINUE_ON_OK=1 auth_preflight_or_report || exit 0
  if BASELANE_STATEMENT_TARGET_YEAR="$TARGET_YEAR" BASELANE_STATEMENT_TARGET_MONTH="$TARGET_MONTH" "$MONTHLY_SCRIPT" && audit_statement_capture; then
    echo "$STAMP" > "$STATE_FILE"
    write_statement_gate_report "ok" "captured-and-verified" "stamp"
    echo "[baselane-monthly] Statements for ${STAMP} captured successfully"
  else
    code=$?
    if [ "$(statement_download_error_class)" = "no-statement-buttons" ]; then
      write_statement_gate_report "review" "no-statement-buttons" "wait-for-statements" "$code"
      echo "[baselane-monthly] Baselane has no ${STAMP} statement download buttons yet; will retry after statements post" >&2
      exit 0
    fi
    write_statement_gate_report "error" "capture-or-verification-failed" "retry-next-run" "$code"
    echo "[baselane-monthly] Statements failed (exit $code), will retry next run" >&2
    exit "$code"
  fi
fi

# Idempotent monthly mortgage workflow verification/application. This runs after statement capture
# so mortgage splits stay attached to the same monthly Baselane close cadence. The child
# script is separately state-gated and verifies current Baselane rows before applying.
if [ "${BASELANE_MONTHLY_SKIP_MORTGAGE_WORKFLOW:-0}" = "1" ]; then
  echo "[baselane-monthly] Mortgage workflow skipped by BASELANE_MONTHLY_SKIP_MORTGAGE_WORKFLOW"
  MORTGAGE_WORKFLOW_STATUS="skipped"
  MORTGAGE_WORKFLOW_SKIPPED_REASON="BASELANE_MONTHLY_SKIP_MORTGAGE_WORKFLOW"
elif [ -x "$MORTGAGE_WORKFLOW_SCRIPT" ]; then
  echo "[baselane-monthly] Running mortgage split workflow gate for ${STAMP}..."
  MORTGAGE_WORKFLOW_STATUS="ran"
  set +e
  "${MORTGAGE_WORKFLOW_TIMEOUT_ARGS[@]}" "$MORTGAGE_WORKFLOW_SCRIPT"
  MORTGAGE_WORKFLOW_RC=$?
  set -e
  if [ "$MORTGAGE_WORKFLOW_RC" -eq 124 ] || [ "$MORTGAGE_WORKFLOW_RC" -eq 137 ]; then
    MORTGAGE_WORKFLOW_STATUS="timeout"
    "$PY" - "$MORTGAGE_WORKFLOW_REPORT" "$MORTGAGE_WORKFLOW_TIMEOUT_SECONDS" "$MORTGAGE_WORKFLOW_KILL_AFTER_SECONDS" "$STAMP" "${MORTGAGE_STATEMENT_TARGET_MONTH:-$STAMP}" "$MORTGAGE_WORKFLOW_RC" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

report_path, timeout_seconds, kill_after_seconds, stamp, target_month, rc = sys.argv[1:]
payload = {
    "job": "baselane-monthly-mortgage-workflow",
    "status": "timeout",
    "reason": "monthly_mortgage_workflow_timeout",
    "rc": int(rc),
    "timeout_seconds": int(timeout_seconds),
    "kill_after_seconds": int(kill_after_seconds),
    "stamp": stamp,
    "mortgage_statement_target_month": target_month,
    "downstream_safe": False,
    "error": "The total mortgage workflow exceeded its bounded runtime; no mortgage workflow result was accepted.",
    "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
}
path = Path(report_path)
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
  fi
  if [ "$MORTGAGE_WORKFLOW_RC" -ne 0 ]; then
    if [ "$MORTGAGE_WORKFLOW_STATUS" = "timeout" ]; then
      echo "[baselane-monthly] Mortgage workflow timed out; downstream publication is blocked" >&2
      write_statement_gate_report "review" "mortgage-workflow-timeout" "mortgage-workflow-timeout" "$MORTGAGE_WORKFLOW_RC"
    else
      echo "[baselane-monthly] Mortgage workflow returned rc=$MORTGAGE_WORKFLOW_RC; statements remain verified, mortgage review is tracked separately" >&2
      write_statement_gate_report "ok" "statements-verified-mortgage-review" "mortgage-review" 0
    fi
  fi
else
  echo "[baselane-monthly] Mortgage workflow script missing or not executable, skipping"
  MORTGAGE_WORKFLOW_STATUS="missing"
  MORTGAGE_WORKFLOW_SKIPPED_REASON="missing_or_not_executable"
fi
if [ -n "$STATEMENT_GATE_REPORTED_STATUS" ]; then
  write_statement_gate_report \
    "$STATEMENT_GATE_REPORTED_STATUS" \
    "$STATEMENT_GATE_REPORTED_REASON" \
    "$STATEMENT_GATE_REPORTED_ACTION" \
    "$STATEMENT_GATE_REPORTED_RC"
fi

# Idempotent former Aligned/AppFolio owner-statement detail import. The importer
# discovers PDFs by content, regenerates staging rows in a per-run workspace, and
# creates only missing manual non-bank Baselane rows keyed by key=aligned-...
# notes. This must run before downstream Baselane ledger exports/Cash Flow syncs.
if [ "$ALIGNED_OWNER_IMPORT_ENABLED" = "1" ] && [ -x "$ALIGNED_OWNER_IMPORT_SCRIPT" ]; then
  if [ "${DRY_RUN:-0}" = "1" ]; then
    ALIGNED_OWNER_IMPORT_APPLY=0
    export BASELANE_ALIGNED_OWNER_IMPORT_APPLY=0
  fi
  if [ "$ALIGNED_OWNER_LIVE_PREFLIGHT_ENABLED" = "1" ] \
    && [ "$ALIGNED_OWNER_IMPORT_APPLY" = "1" ] \
    && [ "${DRY_RUN:-0}" != "1" ]; then
    echo "[baselane-monthly] Verifying Baselane auth before live Aligned import..."
    run_auth_recovery_if_enabled
    if [ "$(auth_recovery_status)" != "ok" ] || [ "$(auth_recovery_manual_auth_required)" = "1" ]; then
      echo "[baselane-monthly] Refusing live Aligned import: Baselane auth is not verified; see $AUTH_RECOVERY_REPORT" >&2
      write_statement_gate_report "review" "aligned-owner-live-auth-required" "auth-baselane" 0
      exit 2
    fi
  fi
  echo "[baselane-monthly] Importing former Aligned owner statement detail for ${TARGET_YEAR}-$(printf '%02d' "$TARGET_MONTH") if needed..."
  aligned_owner_args=(
    --month "${TARGET_YEAR}-$(printf '%02d' "$TARGET_MONTH")"
    --report "$ALIGNED_OWNER_IMPORT_REPORT"
    --manifest-dir "$ALIGNED_OWNER_IMPORT_MANIFEST_DIR"
    --convert
  )
  if [ -n "$ALIGNED_OWNER_IMPORT_CURRENT_PROPERTY_ID" ]; then
    aligned_owner_args+=(--property-id "$ALIGNED_OWNER_IMPORT_CURRENT_PROPERTY_ID")
  fi
  if [ "$ALIGNED_OWNER_IMPORT_APPLY" = "1" ] && [ "${DRY_RUN:-0}" != "1" ]; then
    aligned_owner_args+=(--apply)
  elif [ "${DRY_RUN:-0}" = "1" ]; then
    aligned_owner_args+=(--skip-baselane-query --skip-settlement-relabels)
  fi
  assert_no_apply_in_dry_run "current-month import" "${aligned_owner_args[@]}"
  set +e
  "$PY" "$ALIGNED_OWNER_IMPORT_SCRIPT" "${aligned_owner_args[@]}" >/dev/null
  ALIGNED_OWNER_IMPORT_RC=$?
  set -e
  if [ -f "$ALIGNED_OWNER_IMPORT_REPORT" ]; then
    read -r ALIGNED_OWNER_IMPORT_STATUS ALIGNED_OWNER_IMPORT_CREATED_COUNT ALIGNED_OWNER_IMPORT_TO_CREATE_COUNT ALIGNED_OWNER_IMPORT_SETTLEMENT_CANDIDATE_COUNT < <("$PY" - "$ALIGNED_OWNER_IMPORT_REPORT" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1]))
    print(
        data.get("status") or "unknown",
        int(data.get("created_count") or 0),
        int(data.get("to_create_count") or 0),
        int(data.get("settlement_relabel_candidate_count") or 0),
    )
except Exception:
    print("unknown 0 0 0")
PY
)
  fi
  if [ "$ALIGNED_OWNER_IMPORT_RC" -eq 0 ]; then
    if [ "$ALIGNED_OWNER_IMPORT_CREATED_COUNT" != "0" ]; then
      echo "[baselane-monthly] Aligned owner detail imported; refreshing Baselane ledger export..."
      BASELANE_SYNC_SCRIPT="${BASELANE_SYNC_SCRIPT:-$ROOT/scripts/baselane_sync_cdp_deterministic.py}"
      "$PY" "$BASELANE_SYNC_SCRIPT"
    fi
  elif [ "$ALIGNED_OWNER_IMPORT_RC" -eq 2 ]; then
    echo "[baselane-monthly] Aligned owner statement import needs review; continuing because downstream reports surface the gate: $ALIGNED_OWNER_IMPORT_REPORT" >&2
  else
    echo "[baselane-monthly] Aligned owner statement import failed; see $ALIGNED_OWNER_IMPORT_REPORT" >&2
    exit "$ALIGNED_OWNER_IMPORT_RC"
  fi

	  ALIGNED_OWNER_IMPORT_BACKFILL_CREATED_COUNT=0
	  ALIGNED_OWNER_IMPORT_BACKFILL_TO_CREATE_COUNT=0
	  ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE_COMPLETE=1
	  ALIGNED_OWNER_IMPORT_BACKFILL_SKIP_CREATE_LOOP=0
	  ALIGNED_OWNER_IMPORT_BACKFILL_MONTHS_NORMALIZED="$(printf '%s\n' "$ALIGNED_OWNER_IMPORT_BACKFILL_MONTHS" | tr ',' ' ' | xargs || true)"
  if [ "$ALIGNED_OWNER_LIVE_PREFLIGHT_ENABLED" = "1" ] \
    && [ "$ALIGNED_OWNER_IMPORT_APPLY" = "1" ] \
    && [ "${DRY_RUN:-0}" != "1" ] \
    && [ -n "$ALIGNED_OWNER_IMPORT_BACKFILL_MONTHS_NORMALIZED" ]; then
    echo "[baselane-monthly] Verifying Baselane auth before live Aligned backfill..."
    run_auth_recovery_if_enabled
    if [ "$(auth_recovery_status)" != "ok" ] || [ "$(auth_recovery_manual_auth_required)" = "1" ]; then
      echo "[baselane-monthly] Refusing live Aligned backfill: Baselane auth is not verified; see $AUTH_RECOVERY_REPORT" >&2
      write_statement_gate_report "review" "aligned-owner-live-auth-required" "auth-baselane" 0
      exit 2
    fi
  fi
  if [ "$ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE_ACTIVE" = "1" ] \
    && [ "$ALIGNED_OWNER_PREAPPLY_SCOPE_REVIEW_ENABLED" = "1" ] \
    && [ "$ALIGNED_OWNER_IMPORT_APPLY" = "1" ] \
    && [ "${DRY_RUN:-0}" != "1" ] \
    && [ -n "$ALIGNED_OWNER_IMPORT_BACKFILL_MONTHS_NORMALIZED" ]; then
    if [ ! -x "$ALIGNED_OWNER_SCOPE_REVIEW_SCRIPT" ]; then
      echo "[baselane-monthly] Refusing live Aligned backfill: scope review script missing or not executable: $ALIGNED_OWNER_SCOPE_REVIEW_SCRIPT" >&2
      exit 2
    fi
    mkdir -p "$ALIGNED_OWNER_SCOPE_PREFLIGHT_REPORT_DIR"
    aligned_scope_preflight_args=()
    echo "[baselane-monthly] Running pre-apply Aligned scope dry-run review before live backfill..."
    for aligned_scope_month in $ALIGNED_OWNER_IMPORT_BACKFILL_MONTHS_NORMALIZED; do
      if ! [[ "$aligned_scope_month" =~ ^[0-9]{4}-[0-9]{2}$ ]]; then
        echo "[baselane-monthly] Invalid Aligned scope preflight month: $aligned_scope_month" >&2
        exit 2
      fi
      aligned_scope_preflight_report="$ALIGNED_OWNER_SCOPE_PREFLIGHT_REPORT_DIR/baselane_aligned_owner_statement_import_${aligned_scope_month}.json"
      set +e
      "$PY" "$ALIGNED_OWNER_IMPORT_SCRIPT" \
        --month "$aligned_scope_month" \
        --report "$aligned_scope_preflight_report" \
        --manifest-dir "$ALIGNED_OWNER_IMPORT_MANIFEST_DIR" \
        --convert \
        --skip-baselane-query \
        --skip-settlement-relabels >/dev/null
      aligned_scope_preflight_rc=$?
      set -e
      if [ "$aligned_scope_preflight_rc" -ne 0 ]; then
        echo "[baselane-monthly] Refusing live Aligned backfill: scope preflight dry-run needs review for $aligned_scope_month; see $aligned_scope_preflight_report" >&2
        exit "$aligned_scope_preflight_rc"
      fi
      aligned_scope_preflight_args+=(--dry-run-report "$aligned_scope_preflight_report")
    done
    set +e
    "$PY" "$ALIGNED_OWNER_SCOPE_REVIEW_SCRIPT" \
      --yhome-csv "$ALIGNED_OWNER_YHOME_RECONCILIATION_CSV" \
      --monthly-report "$STATEMENTS_IDEMPOTENT_REPORT" \
      --downstream-report "$ALIGNED_OWNER_DOWNSTREAM_VALIDATION_REPORT" \
      "${aligned_scope_preflight_args[@]}" \
      --report "$ALIGNED_OWNER_SCOPE_REVIEW_REPORT" >/dev/null
    ALIGNED_OWNER_SCOPE_REVIEW_RC=$?
    set -e
    IFS=$'\t' read -r \
      aligned_scope_preflight_status \
      aligned_scope_preflight_review_reason_count \
      aligned_scope_preflight_unqueued_nonzero_count < <("$PY" - "$ALIGNED_OWNER_SCOPE_REVIEW_REPORT" "$ALIGNED_OWNER_SCOPE_REVIEW_RC" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
rc = int(sys.argv[2])
try:
    data = json.loads(path.read_text(encoding="utf-8"))
    status = str(data.get("status") or ("ok" if rc == 0 else "review"))
    if rc != 0 and status not in {"review", "error"}:
        status = "review"
    coverage = data.get("import_coverage") if isinstance(data.get("import_coverage"), dict) else {}
    print(
        status,
        len(data.get("review_reasons") or []),
        int(coverage.get("unqueued_nonzero_property_count") or 0),
        sep="\t",
    )
except Exception:
    print("review", 1, 1, sep="\t")
PY
)
    if [ "$ALIGNED_OWNER_SCOPE_REVIEW_RC" -ne 0 ] \
      || [ "$aligned_scope_preflight_review_reason_count" != "0" ] \
      || [ "$aligned_scope_preflight_unqueued_nonzero_count" != "0" ]; then
      echo "[baselane-monthly] Refusing live Aligned backfill: pre-apply scope review is ${aligned_scope_preflight_status}; see $ALIGNED_OWNER_SCOPE_REVIEW_REPORT" >&2
      exit 2
    fi
  fi
  if [ "$ALIGNED_OWNER_LIVE_PREFLIGHT_ENABLED" = "1" ] \
    && [ "$ALIGNED_OWNER_IMPORT_APPLY" = "1" ] \
    && [ "${DRY_RUN:-0}" != "1" ] \
    && [ -n "$ALIGNED_OWNER_IMPORT_BACKFILL_MONTHS_NORMALIZED" ]; then
    mkdir -p "$ALIGNED_OWNER_LIVE_PREFLIGHT_REPORT_DIR"
    echo "[baselane-monthly] Running no-apply Baselane duplicate preflight before live Aligned backfill..."
    aligned_live_preflight_args=(
      --queue "$ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE"
      --config "$ROOT/config/aligned_owner_statement_imports.json"
      --importer "$ALIGNED_OWNER_IMPORT_SCRIPT"
      --manifest-dir "$ALIGNED_OWNER_IMPORT_MANIFEST_DIR"
      --report-dir "$ALIGNED_OWNER_LIVE_PREFLIGHT_REPORT_DIR"
      --report "$ALIGNED_OWNER_LIVE_PREFLIGHT_SUMMARY_REPORT"
      --python-bin "$PY"
      --per-month-timeout-seconds "$ALIGNED_OWNER_PREFLIGHT_MONTH_TIMEOUT_SECONDS"
      --staging-fallback-timeout-seconds "$ALIGNED_OWNER_PREFLIGHT_STAGING_FALLBACK_TIMEOUT_SECONDS"
    )
    if [ -n "$ALIGNED_OWNER_IMPORT_BACKFILL_PROPERTY_ID" ]; then
      aligned_live_preflight_args+=(--property-id "$ALIGNED_OWNER_IMPORT_BACKFILL_PROPERTY_ID")
    fi
    run_aligned_live_queue_preflight
    read -r aligned_live_preflight_status aligned_live_preflight_reasons aligned_live_preflight_existing aligned_live_preflight_to_create aligned_live_preflight_auth_like < <(aligned_live_preflight_summary_fields "$ALIGNED_OWNER_LIVE_PREFLIGHT_SUMMARY_REPORT")
    normalize_aligned_live_preflight_reasons
    if [ "$aligned_live_preflight_existing" != "0" ] && [ "$aligned_live_preflight_status" = "ok" ] && [[ "$aligned_live_preflight_reasons" != *"duplicate_keys_present"* ]]; then
      echo "[baselane-monthly] Aligned duplicate preflight found $aligned_live_preflight_existing expected queued keys already present; retry is idempotent and importer will skip them."
    elif [ "$aligned_live_preflight_existing" != "0" ] || [[ "$aligned_live_preflight_reasons" == *"duplicate_keys_present"* ]]; then
      echo "[baselane-monthly] Refusing live Aligned backfill: $aligned_live_preflight_existing queued Aligned keys already exist in Baselane; see $ALIGNED_OWNER_LIVE_PREFLIGHT_SUMMARY_REPORT" >&2
      write_statement_gate_report "review" "aligned-owner-live-duplicate-keys-present" "review-aligned-owner-import" 0
      exit 2
    fi
    if [ "$aligned_live_preflight_status" != "ok" ] || [ "$aligned_live_preflight_rc" -ne 0 ]; then
      if [ "$aligned_live_preflight_auth_like" = "1" ]; then
        echo "[baselane-monthly] Aligned duplicate preflight hit an auth-like Baselane query failure; refreshing auth and retrying once..."
        run_auth_recovery_if_enabled
        if [ "$(auth_recovery_status)" != "ok" ] || [ "$(auth_recovery_manual_auth_required)" = "1" ]; then
          echo "[baselane-monthly] Refusing live Aligned backfill: Baselane auth is not verified after duplicate preflight recovery; see $AUTH_RECOVERY_REPORT" >&2
          write_statement_gate_report "review" "aligned-owner-live-auth-required" "auth-baselane" 0
          exit 2
        fi
        run_aligned_live_queue_preflight
        read -r aligned_live_preflight_status aligned_live_preflight_reasons aligned_live_preflight_existing aligned_live_preflight_to_create aligned_live_preflight_auth_like < <(aligned_live_preflight_summary_fields "$ALIGNED_OWNER_LIVE_PREFLIGHT_SUMMARY_REPORT")
        normalize_aligned_live_preflight_reasons
        if [ "$aligned_live_preflight_existing" != "0" ] && [ "$aligned_live_preflight_status" = "ok" ] && [[ "$aligned_live_preflight_reasons" != *"duplicate_keys_present"* ]]; then
          echo "[baselane-monthly] Aligned duplicate preflight retry found $aligned_live_preflight_existing expected queued keys already present; retry remains idempotent."
        elif [ "$aligned_live_preflight_existing" != "0" ] || [[ "$aligned_live_preflight_reasons" == *"duplicate_keys_present"* ]]; then
          echo "[baselane-monthly] Refusing live Aligned backfill: $aligned_live_preflight_existing queued Aligned keys already exist in Baselane after auth retry; see $ALIGNED_OWNER_LIVE_PREFLIGHT_SUMMARY_REPORT" >&2
          write_statement_gate_report "review" "aligned-owner-live-duplicate-keys-present" "review-aligned-owner-import" 0
          exit 2
        fi
      fi
    fi
    if [ "$aligned_live_preflight_status" != "ok" ] || [ "$aligned_live_preflight_rc" -ne 0 ]; then
      echo "[baselane-monthly] Refusing live Aligned backfill: no-apply duplicate preflight needs review (${aligned_live_preflight_reasons:-unknown}); see $ALIGNED_OWNER_LIVE_PREFLIGHT_SUMMARY_REPORT" >&2
      write_statement_gate_report "review" "aligned-owner-live-duplicate-preflight-review" "review-aligned-owner-import" 0
      exit 2
    fi
	    if [ "$aligned_live_preflight_to_create" = "0" ] && [ "$aligned_live_preflight_existing" != "0" ] && [ "$aligned_live_preflight_status" = "ok" ]; then
	      echo "[baselane-monthly] Aligned duplicate preflight found no remaining rows to create because expected queued keys are already present; continuing to validation."
	      ALIGNED_OWNER_IMPORT_BACKFILL_SKIP_CREATE_LOOP=1
	    elif [ "$aligned_live_preflight_to_create" = "0" ]; then
	      echo "[baselane-monthly] Refusing live Aligned backfill: queue has no rows to create after duplicate preflight; see $ALIGNED_OWNER_LIVE_PREFLIGHT_SUMMARY_REPORT" >&2
      write_statement_gate_report "review" "aligned-owner-live-no-rows-to-create" "review-aligned-owner-import" 0
      exit 2
    fi
  fi
  if [ "$ALIGNED_OWNER_LIVE_PREFLIGHT_ENABLED" = "1" ] \
    && [ "${DRY_RUN:-0}" = "1" ] \
    && [ -n "$ALIGNED_OWNER_IMPORT_BACKFILL_MONTHS_NORMALIZED" ]; then
    mkdir -p "$ALIGNED_OWNER_LIVE_PREFLIGHT_REPORT_DIR"
    echo "[baselane-monthly] Running read-only Baselane duplicate preflight for dry-run Aligned backfill..."
    aligned_live_preflight_args=(
      --queue "$ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE"
      --config "$ROOT/config/aligned_owner_statement_imports.json"
      --importer "$ALIGNED_OWNER_IMPORT_SCRIPT"
      --manifest-dir "$ALIGNED_OWNER_IMPORT_MANIFEST_DIR"
      --report-dir "$ALIGNED_OWNER_LIVE_PREFLIGHT_REPORT_DIR"
      --report "$ALIGNED_OWNER_LIVE_PREFLIGHT_SUMMARY_REPORT"
      --python-bin "$PY"
      --per-month-timeout-seconds "$ALIGNED_OWNER_PREFLIGHT_MONTH_TIMEOUT_SECONDS"
      --staging-fallback-timeout-seconds "$ALIGNED_OWNER_PREFLIGHT_STAGING_FALLBACK_TIMEOUT_SECONDS"
    )
    if [ -n "$ALIGNED_OWNER_IMPORT_BACKFILL_PROPERTY_ID" ]; then
      aligned_live_preflight_args+=(--property-id "$ALIGNED_OWNER_IMPORT_BACKFILL_PROPERTY_ID")
    fi
    set +e
    "$PY" "$ALIGNED_OWNER_QUEUE_PREFLIGHT_SCRIPT" "${aligned_live_preflight_args[@]}" >/dev/null
    aligned_live_preflight_rc=$?
    set -e
    if [ "$aligned_live_preflight_rc" -ne 0 ]; then
      echo "[baselane-monthly] Dry-run Aligned duplicate preflight needs review; continuing because no live writes are enabled: $ALIGNED_OWNER_LIVE_PREFLIGHT_SUMMARY_REPORT" >&2
    fi
  fi
	  if [ -n "$ALIGNED_OWNER_IMPORT_BACKFILL_MONTHS_NORMALIZED" ] && [ "$ALIGNED_OWNER_IMPORT_BACKFILL_SKIP_CREATE_LOOP" != "1" ]; then
	    mkdir -p "$ALIGNED_OWNER_IMPORT_BACKFILL_REPORT_DIR"
	    echo "[baselane-monthly] Running opt-in Aligned owner statement backfill months: $ALIGNED_OWNER_IMPORT_BACKFILL_MONTHS_NORMALIZED"
    for aligned_backfill_month in $ALIGNED_OWNER_IMPORT_BACKFILL_MONTHS_NORMALIZED; do
      if ! [[ "$aligned_backfill_month" =~ ^[0-9]{4}-[0-9]{2}$ ]]; then
        echo "[baselane-monthly] Invalid Aligned backfill month: $aligned_backfill_month" >&2
        exit 2
      fi
      if [ "$aligned_backfill_month" = "${TARGET_YEAR}-$(printf '%02d' "$TARGET_MONTH")" ]; then
        echo "[baselane-monthly] Skipping Aligned backfill month already handled as current target: $aligned_backfill_month"
        continue
      fi
      aligned_backfill_report="$ALIGNED_OWNER_IMPORT_BACKFILL_REPORT_DIR/baselane_aligned_owner_statement_import_${aligned_backfill_month}.json"
      aligned_backfill_args=(
        --month "$aligned_backfill_month"
        --report "$aligned_backfill_report"
        --manifest-dir "$ALIGNED_OWNER_IMPORT_MANIFEST_DIR"
        --expected-plan-queue "$ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE"
        --convert
      )
      if [ -n "$ALIGNED_OWNER_IMPORT_BACKFILL_PROPERTY_ID" ]; then
        aligned_backfill_args+=(--property-id "$ALIGNED_OWNER_IMPORT_BACKFILL_PROPERTY_ID")
      fi
      if [ "$ALIGNED_OWNER_IMPORT_APPLY" = "1" ] && [ "${DRY_RUN:-0}" != "1" ]; then
        aligned_backfill_args+=(--apply)
      elif [ "${DRY_RUN:-0}" = "1" ]; then
        aligned_backfill_args+=(--skip-baselane-query --skip-settlement-relabels)
      fi
      assert_no_apply_in_dry_run "backfill import $aligned_backfill_month" "${aligned_backfill_args[@]}"
      set +e
      "$PY" "$ALIGNED_OWNER_IMPORT_SCRIPT" "${aligned_backfill_args[@]}" >/dev/null
      aligned_backfill_rc=$?
      set -e
      if [ -f "$aligned_backfill_report" ]; then
        read -r aligned_backfill_status aligned_backfill_created aligned_backfill_to_create < <("$PY" - "$aligned_backfill_report" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1]))
    print(data.get("status") or "unknown", int(data.get("created_count") or 0), int(data.get("to_create_count") or 0))
except Exception:
    print("unknown 0 0")
PY
)
        ALIGNED_OWNER_IMPORT_BACKFILL_CREATED_COUNT=$((ALIGNED_OWNER_IMPORT_BACKFILL_CREATED_COUNT + aligned_backfill_created))
        ALIGNED_OWNER_IMPORT_BACKFILL_TO_CREATE_COUNT=$((ALIGNED_OWNER_IMPORT_BACKFILL_TO_CREATE_COUNT + aligned_backfill_to_create))
      else
        aligned_backfill_status="missing_report"
      fi
      if [ "$aligned_backfill_rc" -eq 0 ]; then
        :
      elif [ "$aligned_backfill_rc" -eq 2 ]; then
        ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE_COMPLETE=0
        echo "[baselane-monthly] Aligned owner backfill $aligned_backfill_month needs review; continuing: $aligned_backfill_report" >&2
      else
        echo "[baselane-monthly] Aligned owner backfill $aligned_backfill_month failed ($aligned_backfill_status); see $aligned_backfill_report" >&2
        exit "$aligned_backfill_rc"
      fi
    done
    if [ "$ALIGNED_OWNER_IMPORT_BACKFILL_CREATED_COUNT" != "0" ]; then
      echo "[baselane-monthly] Aligned owner backfill imported rows; refreshing Baselane ledger export..."
      BASELANE_SYNC_SCRIPT="${BASELANE_SYNC_SCRIPT:-$ROOT/scripts/baselane_sync_cdp_deterministic.py}"
      "$PY" "$BASELANE_SYNC_SCRIPT"
    fi
	    if [ "$ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE_ACTIVE" = "1" ] && [ "${DRY_RUN:-0}" != "1" ] && [ "$ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE_COMPLETE" = "1" ]; then
	      echo "[baselane-monthly] Aligned owner backfill import loop succeeded; queue completion deferred until downstream validation is ok."
	    fi
	  elif [ -n "$ALIGNED_OWNER_IMPORT_BACKFILL_MONTHS_NORMALIZED" ]; then
	    echo "[baselane-monthly] Skipping Aligned backfill import loop because live duplicate preflight found all expected queued keys already present."
	  fi
  export BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_BACKFILL_MONTHS="$ALIGNED_OWNER_IMPORT_BACKFILL_MONTHS_NORMALIZED"
  export BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_BACKFILL_REPORT_DIR="$ALIGNED_OWNER_IMPORT_BACKFILL_REPORT_DIR"
  export BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_BACKFILL_CREATED_COUNT="$ALIGNED_OWNER_IMPORT_BACKFILL_CREATED_COUNT"
  export BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_BACKFILL_TO_CREATE_COUNT="$ALIGNED_OWNER_IMPORT_BACKFILL_TO_CREATE_COUNT"
else
  echo "[baselane-monthly] Aligned owner statement import disabled or missing, skipping"
  ALIGNED_OWNER_IMPORT_STATUS="skipped"
  export BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_BACKFILL_MONTHS=""
  export BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_BACKFILL_REPORT_DIR="$ALIGNED_OWNER_IMPORT_BACKFILL_REPORT_DIR"
  export BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_BACKFILL_CREATED_COUNT="0"
  export BASELANE_STATEMENT_GATE_ALIGNED_OWNER_IMPORT_BACKFILL_TO_CREATE_COUNT="0"
fi

ALIGNED_OWNER_CF_SYNC_QUEUE_REQUESTED=0
if [ "$ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE_ACTIVE" = "1" ] || [ -n "${ALIGNED_OWNER_IMPORT_BACKFILL_MONTHS_NORMALIZED:-}" ]; then
  ALIGNED_OWNER_CF_SYNC_QUEUE_REQUESTED=1
fi
if [ "$ALIGNED_OWNER_CF_SYNC_ENABLED" = "1" ] \
  && [ -f "$ALIGNED_OWNER_CF_SYNC_SCRIPT" ] \
  && [ -f "$ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE" ] \
  && [ "$ALIGNED_OWNER_CF_SYNC_QUEUE_REQUESTED" = "1" ]; then
  IFS=$'\t' read -r ALIGNED_OWNER_CF_SYNC_PROPERTY_SCOPE ALIGNED_OWNER_CF_SYNC_MONTHS < <(
    "$PY" - "$ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE" "$ROOT/config/aligned_owner_statement_imports.json" "${ALIGNED_OWNER_IMPORT_BACKFILL_MONTHS_NORMALIZED:-}" <<'PY'
import json
import sys
from pathlib import Path

queue_path = Path(sys.argv[1])
config_path = Path(sys.argv[2])
backfill_months = [month for month in sys.argv[3].split() if month]
try:
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
except Exception:
    queue = {}
try:
    config = json.loads(config_path.read_text(encoding="utf-8"))
except Exception:
    config = {}

expected = queue.get("expected") or {}
property_id = str(expected.get("baselane_property_id") or "")
property_scope = str(expected.get("property") or "")
for item in config.get("properties") or []:
    if str(item.get("baselane_property_id") or "") == property_id:
        property_scope = str(item.get("property_short") or item.get("property_full") or property_scope)
        break
months = backfill_months or [str(month) for month in queue.get("months") or [] if str(month).strip()]
print(f"{property_scope}\t{' '.join(months)}")
PY
  )
  if [ -n "${ALIGNED_OWNER_CF_SYNC_PROPERTY_SCOPE:-}" ] && [ -n "${ALIGNED_OWNER_CF_SYNC_MONTHS:-}" ]; then
    if [ ! -f "$ALIGNED_OWNER_CF_SYNC_LEDGER" ]; then
      aligned_owner_cf_sync_ledger_candidates=(
        "$ROOT/reports/baselane_source_transaction_index.csv" \
        "/mnt/c/Users/digit/Dropbox/Projects/assetrail/ECO Systems General Ledger.csv" \
        "/home/digit/Dropbox/Projects/assetrail/ECO Systems General Ledger.csv"
      )
      for candidate in "${aligned_owner_cf_sync_ledger_candidates[@]}"; do
        if [ -f "$candidate" ]; then
          ALIGNED_OWNER_CF_SYNC_LEDGER="$candidate"
          break
        fi
      done
    fi
    mkdir -p "$(dirname "$ALIGNED_OWNER_CF_SYNC_REPORT")" "$(dirname "$ALIGNED_OWNER_CF_SYNC_LOG")"
    cf_sync_results_tsv="$REPORT_DIR/.aligned_owner_statement_cf_sync_results.tsv"
    : > "$cf_sync_results_tsv"
    ALIGNED_OWNER_CF_SYNC_STATUS="ok"
    ALIGNED_OWNER_CF_SYNC_RC=0
    ALIGNED_OWNER_CF_SYNC_MONTH_COUNT=0
    echo "[baselane-monthly] Running property-scoped Aligned CF sync for ${ALIGNED_OWNER_CF_SYNC_PROPERTY_SCOPE}: ${ALIGNED_OWNER_CF_SYNC_MONTHS}"
    for aligned_cf_month in $ALIGNED_OWNER_CF_SYNC_MONTHS; do
      if ! [[ "$aligned_cf_month" =~ ^[0-9]{4}-[0-9]{2}$ ]]; then
        ALIGNED_OWNER_CF_SYNC_STATUS="review"
        ALIGNED_OWNER_CF_SYNC_RC=2
        printf '%s\t%s\t%s\t%s\t%s\n' "$aligned_cf_month" "2" "invalid_month" "" "" >> "$cf_sync_results_tsv"
        continue
      fi
      cf_sync_args=(
        --month "$aligned_cf_month"
        --property "$ALIGNED_OWNER_CF_SYNC_PROPERTY_SCOPE"
        --gl-csv "$ALIGNED_OWNER_CF_SYNC_LEDGER"
        --source-cash-gl-csv "$ALIGNED_OWNER_CF_SYNC_LEDGER"
        --output-dir "$REAL_ESTATE_ROOT"
        --conflict-threshold 0.01
      )
      if [ "${DRY_RUN:-0}" = "1" ]; then
        cf_sync_args+=(--audit)
      fi
      set +e
      "$PY" "$ALIGNED_OWNER_CF_SYNC_SCRIPT" "${cf_sync_args[@]}" >>"$ALIGNED_OWNER_CF_SYNC_LOG" 2>&1
      aligned_cf_rc=$?
      set -e
      ALIGNED_OWNER_CF_SYNC_MONTH_COUNT=$((ALIGNED_OWNER_CF_SYNC_MONTH_COUNT + 1))
      if [ "$aligned_cf_rc" -ne 0 ]; then
        ALIGNED_OWNER_CF_SYNC_STATUS="review"
        ALIGNED_OWNER_CF_SYNC_RC="$aligned_cf_rc"
      fi
      printf '%s\t%s\t%s\t%s\t%s\n' \
        "$aligned_cf_month" \
        "$aligned_cf_rc" \
        "$([ "${DRY_RUN:-0}" = "1" ] && printf audit || printf apply)" \
        "$ROOT/reports/cf_statement_sync/audit_${aligned_cf_month}.json" \
        "$ROOT/reports/cf_statement_sync/discovery_${aligned_cf_month}.json" >> "$cf_sync_results_tsv"
    done
    "$PY" - "$ALIGNED_OWNER_CF_SYNC_REPORT" "$cf_sync_results_tsv" "$ALIGNED_OWNER_CF_SYNC_PROPERTY_SCOPE" "$ALIGNED_OWNER_CF_SYNC_LEDGER" "$ALIGNED_OWNER_CF_SYNC_STATUS" "$ALIGNED_OWNER_CF_SYNC_RC" "$ALIGNED_OWNER_CF_SYNC_MONTH_COUNT" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

report_path = Path(sys.argv[1])
results_path = Path(sys.argv[2])
property_scope = sys.argv[3]
ledger = sys.argv[4]
status = sys.argv[5]
rc = int(sys.argv[6])
month_count = int(sys.argv[7])
rows = []
if results_path.exists():
    for line in results_path.read_text(encoding="utf-8").splitlines():
        month, row_rc, mode, audit, discovery = (line.split("\t") + ["", "", "", "", ""])[:5]
        rows.append({
            "month": month,
            "return_code": int(row_rc or 0),
            "mode": mode,
            "audit_report": audit or None,
            "discovery_report": discovery or None,
            "audit_report_exists": bool(audit and Path(audit).is_file()),
            "discovery_report_exists": bool(discovery and Path(discovery).is_file()),
        })
report = {
    "job": "aligned-owner-statement-cf-sync",
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "status": status,
    "return_code": rc,
    "property_scope": property_scope,
    "ledger": ledger,
    "month_count": month_count,
    "months": rows,
}
report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
    rm -f "$cf_sync_results_tsv"
    if [ "$ALIGNED_OWNER_CF_SYNC_STATUS" != "ok" ]; then
      echo "[baselane-monthly] Aligned owner CF sync needs review; continuing because report is explicit: $ALIGNED_OWNER_CF_SYNC_REPORT" >&2
    fi
  else
    ALIGNED_OWNER_CF_SYNC_STATUS="skipped_no_property_scope"
  fi
else
  if [ "$ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE_STATUS" = "completed" ] && [ "$ALIGNED_OWNER_CF_SYNC_QUEUE_REQUESTED" = "0" ]; then
    ALIGNED_OWNER_CF_SYNC_STATUS="skipped_completed_queue"
  else
    ALIGNED_OWNER_CF_SYNC_STATUS="skipped"
  fi
fi

if [ -f "$ALIGNED_OWNER_DOWNSTREAM_VALIDATION_SCRIPT" ] && [ -f "$ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE" ]; then
  echo "[baselane-monthly] Validating Aligned owner statement downstream ledger/CF coverage..."
  set +e
  "$PY" "$ALIGNED_OWNER_DOWNSTREAM_VALIDATION_SCRIPT" \
    --queue "$ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE" \
    --config "$ROOT/config/aligned_owner_statement_imports.json" \
    --manifest-dir "$ALIGNED_OWNER_IMPORT_MANIFEST_DIR" \
    --cf-sync-report "$ALIGNED_OWNER_CF_SYNC_REPORT" \
    --report "$ALIGNED_OWNER_DOWNSTREAM_VALIDATION_REPORT" >/dev/null
  ALIGNED_OWNER_DOWNSTREAM_VALIDATION_RC=$?
  set -e
  if [ -f "$ALIGNED_OWNER_DOWNSTREAM_VALIDATION_REPORT" ]; then
    read -r \
      ALIGNED_OWNER_DOWNSTREAM_VALIDATION_STATUS \
      ALIGNED_OWNER_DOWNSTREAM_VALIDATION_FOUND_KEY_COUNT \
      ALIGNED_OWNER_DOWNSTREAM_VALIDATION_MISSING_KEY_COUNT < <("$PY" - "$ALIGNED_OWNER_DOWNSTREAM_VALIDATION_REPORT" <<'PY'
import json
import sys

try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
    ledger = data.get("ledger_presence") or {}
    print(
        data.get("status") or "unknown",
        int(ledger.get("found_key_count") or 0),
        int(ledger.get("missing_key_count") or 0),
    )
except Exception:
    print("unknown 0 0")
PY
)
  fi
  if [ "$ALIGNED_OWNER_DOWNSTREAM_VALIDATION_RC" -ne 0 ]; then
    echo "[baselane-monthly] Aligned owner downstream validation needs review; continuing because report is non-mutating: $ALIGNED_OWNER_DOWNSTREAM_VALIDATION_REPORT" >&2
  fi
else
  ALIGNED_OWNER_DOWNSTREAM_VALIDATION_STATUS="skipped"
fi
if [ "$ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE_ACTIVE" = "1" ] \
  && [ "${DRY_RUN:-0}" != "1" ] \
  && [ "${ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE_COMPLETE:-0}" = "1" ]; then
  if [ "$ALIGNED_OWNER_DOWNSTREAM_VALIDATION_STATUS" = "ok" ]; then
    "$PY" - \
      "$ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE" \
      "$ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE_ID" \
      "${ALIGNED_OWNER_IMPORT_BACKFILL_MONTHS_NORMALIZED:-}" \
      "${ALIGNED_OWNER_IMPORT_BACKFILL_CREATED_COUNT:-0}" \
      "${ALIGNED_OWNER_IMPORT_BACKFILL_TO_CREATE_COUNT:-0}" \
      "$ALIGNED_OWNER_IMPORT_BACKFILL_REPORT_DIR" \
      "$ALIGNED_OWNER_DOWNSTREAM_VALIDATION_REPORT" \
      "$ALIGNED_OWNER_CF_SYNC_REPORT" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
queue_id = sys.argv[2]
months = [month for month in sys.argv[3].split() if month]
created_count = int(sys.argv[4])
to_create_count = int(sys.argv[5])
report_dir = sys.argv[6]
validation_report = sys.argv[7]
cf_sync_report = sys.argv[8]

try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    data = {}

data["queue_id"] = data.get("queue_id") or queue_id
data["status"] = "completed"
data["completed_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
data["completed_by"] = "baselane_monthly_statements_idempotent.sh"
data["completion_gate"] = "downstream_validation_ok"
data["completed_months"] = months
data["created_count"] = created_count
data["to_create_count_at_completion"] = to_create_count
data["backfill_report_dir"] = report_dir
data["downstream_validation_report"] = validation_report
data["cf_sync_report"] = cf_sync_report
path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
    ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE_STATUS="completed"
    echo "[baselane-monthly] Completed Aligned owner backfill queue after downstream validation ok: $ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE"
  else
    echo "[baselane-monthly] Leaving Aligned owner backfill queue queued; downstream validation status is ${ALIGNED_OWNER_DOWNSTREAM_VALIDATION_STATUS}." >&2
  fi
fi
ALIGNED_OWNER_WORKFLOW_ACTIVE=0
if [ "$ALIGNED_OWNER_IMPORT_STATUS" != "skipped" ] \
  || [ "$ALIGNED_OWNER_CF_SYNC_STATUS" != "skipped" ] \
  || [ "$ALIGNED_OWNER_DOWNSTREAM_VALIDATION_STATUS" != "skipped" ]; then
  ALIGNED_OWNER_WORKFLOW_ACTIVE=1
  write_statement_gate_report "ok" "statements-verified-aligned-owner-import" "aligned-owner-import" 0
fi
if [ -x "$ALIGNED_OWNER_SCOPE_REVIEW_SCRIPT" ]; then
  echo "[baselane-monthly] Refreshing Cleveland/Hemlane Aligned owner statement review artifact..."
  set +e
  "$PY" "$ALIGNED_OWNER_SCOPE_REVIEW_SCRIPT" \
    --yhome-csv "$ALIGNED_OWNER_YHOME_RECONCILIATION_CSV" \
    --monthly-report "$STATEMENTS_IDEMPOTENT_REPORT" \
    --downstream-report "$ALIGNED_OWNER_DOWNSTREAM_VALIDATION_REPORT" \
    --report "$ALIGNED_OWNER_SCOPE_REVIEW_REPORT" >/dev/null
  ALIGNED_OWNER_SCOPE_REVIEW_RC=$?
  set -e
  if [ -f "$ALIGNED_OWNER_SCOPE_REVIEW_REPORT" ]; then
    IFS=$'\t' read -r \
      ALIGNED_OWNER_SCOPE_REVIEW_STATUS \
      ALIGNED_OWNER_SCOPE_REVIEW_SCOPE_CANDIDATE_COUNT \
      ALIGNED_OWNER_SCOPE_REVIEW_COMPLETION_STATE < <("$PY" - "$ALIGNED_OWNER_SCOPE_REVIEW_REPORT" "$ALIGNED_OWNER_SCOPE_REVIEW_RC" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
rc = int(sys.argv[2])
try:
    data = json.loads(path.read_text(encoding="utf-8"))
    status = str(data.get("status") or ("ok" if rc == 0 else "review"))
    if rc != 0 and status not in {"review", "error"}:
        status = "review"
    print(
        status,
        len(data.get("scope_candidates") or []),
        data.get("completion_state") or "",
        sep="\t",
    )
except Exception:
    print("review", 0, "", sep="\t")
PY
)
  else
    ALIGNED_OWNER_SCOPE_REVIEW_STATUS="review"
  fi
  if [ "$ALIGNED_OWNER_SCOPE_REVIEW_RC" -ne 0 ]; then
    echo "[baselane-monthly] Cleveland/Hemlane Aligned review needs review; continuing because report is non-mutating: $ALIGNED_OWNER_SCOPE_REVIEW_REPORT" >&2
  fi
  if [ "$ALIGNED_OWNER_WORKFLOW_ACTIVE" = "1" ]; then
    write_statement_gate_report "ok" "statements-verified-aligned-owner-import" "aligned-owner-import" 0
  fi
else
  ALIGNED_OWNER_SCOPE_REVIEW_STATUS="skipped"
fi
if [ "$ALIGNED_OWNER_WORKFLOW_ACTIVE" = "1" ] && [ -f "$ALIGNED_OWNER_COMPLETION_GATE_SCRIPT" ]; then
  echo "[baselane-monthly] Running read-only Aligned owner completion gate..."
  set +e
  "$PY" "$ALIGNED_OWNER_COMPLETION_GATE_SCRIPT" \
    --no-refresh-downstream \
    --queue "$ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE" \
    --config "$ROOT/config/aligned_owner_statement_imports.json" \
    --manifest-dir "$ALIGNED_OWNER_IMPORT_MANIFEST_DIR" \
    --cf-sync-report "$ALIGNED_OWNER_CF_SYNC_REPORT" \
    --downstream-report "$ALIGNED_OWNER_DOWNSTREAM_VALIDATION_REPORT" \
    --scope-review "$ALIGNED_OWNER_SCOPE_REVIEW_REPORT" \
    --report "$ALIGNED_OWNER_COMPLETION_GATE_REPORT" >/dev/null
  ALIGNED_OWNER_COMPLETION_GATE_RC=$?
  set -e
  if [ -f "$ALIGNED_OWNER_COMPLETION_GATE_REPORT" ]; then
    IFS=$'\t' read -r \
      ALIGNED_OWNER_COMPLETION_GATE_STATUS \
      ALIGNED_OWNER_COMPLETION_GATE_REVIEW_REASON_COUNT < <("$PY" - "$ALIGNED_OWNER_COMPLETION_GATE_REPORT" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    data = json.loads(path.read_text(encoding="utf-8"))
    print(data.get("status") or "unknown", len(data.get("review_reasons") or []), sep="\t")
except Exception:
    print("unknown", 0, sep="\t")
PY
)
  else
    ALIGNED_OWNER_COMPLETION_GATE_STATUS="missing_report"
  fi
  if [ "$ALIGNED_OWNER_COMPLETION_GATE_RC" -ne 0 ]; then
    echo "[baselane-monthly] Aligned owner completion gate is not complete yet; report: $ALIGNED_OWNER_COMPLETION_GATE_REPORT" >&2
  fi
  write_statement_gate_report "ok" "statements-verified-aligned-owner-import" "aligned-owner-import" 0
else
  ALIGNED_OWNER_COMPLETION_GATE_STATUS="skipped"
fi
if [ "$ALIGNED_OWNER_WORKFLOW_ACTIVE" = "1" ] && [ -f "$ALIGNED_OWNER_IMPORT_READINESS_SCRIPT" ]; then
  echo "[baselane-monthly] Refreshing read-only Aligned owner import readiness artifact..."
  set +e
  timeout --kill-after=30s "${ALIGNED_OWNER_IMPORT_READINESS_TIMEOUT_SECONDS}s" \
    "$PY" "$ALIGNED_OWNER_IMPORT_READINESS_SCRIPT" \
    --queue "$ALIGNED_OWNER_IMPORT_BACKFILL_QUEUE" \
    --config "$ROOT/config/aligned_owner_statement_imports.json" \
    --auth-report "$AUTH_RECOVERY_REPORT" \
    --preflight-summary "$ALIGNED_OWNER_LIVE_PREFLIGHT_SUMMARY_REPORT" \
    --downstream-report "$ALIGNED_OWNER_DOWNSTREAM_VALIDATION_REPORT" \
    --scope-review "$ALIGNED_OWNER_SCOPE_REVIEW_REPORT" \
    --completion-report "$ALIGNED_OWNER_COMPLETION_GATE_REPORT" \
    --import-report-dir "$ALIGNED_OWNER_IMPORT_BACKFILL_REPORT_DIR" \
    --report "$ALIGNED_OWNER_IMPORT_READINESS_REPORT" \
    --markdown "$ALIGNED_OWNER_IMPORT_READINESS_MARKDOWN" >/dev/null
  ALIGNED_OWNER_IMPORT_READINESS_RC=$?
  set -e
  if [ "$ALIGNED_OWNER_IMPORT_READINESS_RC" -eq 124 ] || [ "$ALIGNED_OWNER_IMPORT_READINESS_RC" -eq 137 ]; then
    ALIGNED_OWNER_IMPORT_READINESS_STATUS="review_timeout"
    ALIGNED_OWNER_IMPORT_READINESS_READY=0
    if [ "$ALIGNED_OWNER_IMPORT_READINESS_RC" -eq 137 ]; then
      ALIGNED_OWNER_IMPORT_READINESS_PRIMARY_BLOCKER="readiness_killed_after_timeout_${ALIGNED_OWNER_IMPORT_READINESS_TIMEOUT_SECONDS}s"
    else
      ALIGNED_OWNER_IMPORT_READINESS_PRIMARY_BLOCKER="readiness_timeout_${ALIGNED_OWNER_IMPORT_READINESS_TIMEOUT_SECONDS}s"
    fi
  elif [ -f "$ALIGNED_OWNER_IMPORT_READINESS_REPORT" ]; then
    IFS=$'\t' read -r \
      ALIGNED_OWNER_IMPORT_READINESS_STATUS \
      ALIGNED_OWNER_IMPORT_READINESS_READY \
      ALIGNED_OWNER_IMPORT_READINESS_PRIMARY_BLOCKER < <("$PY" - "$ALIGNED_OWNER_IMPORT_READINESS_REPORT" "$ALIGNED_OWNER_IMPORT_READINESS_RC" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
rc = int(sys.argv[2])
try:
    data = json.loads(path.read_text(encoding="utf-8"))
    status = str(data.get("status") or ("ok" if rc == 0 else "review"))
    ready = "1" if data.get("ready_for_cron_live_import") is True else "0"
    blocker = str(data.get("primary_blocker") or "")
    print(status, ready, blocker, sep="\t")
except Exception:
    print("review", "0", "readiness_report_unreadable", sep="\t")
PY
)
  else
    ALIGNED_OWNER_IMPORT_READINESS_STATUS="missing_report"
    ALIGNED_OWNER_IMPORT_READINESS_READY=0
    ALIGNED_OWNER_IMPORT_READINESS_PRIMARY_BLOCKER="readiness_report_missing"
  fi
  if [ "$ALIGNED_OWNER_IMPORT_READINESS_RC" -ne 0 ]; then
    echo "[baselane-monthly] Aligned owner import readiness is not ready; report: $ALIGNED_OWNER_IMPORT_READINESS_REPORT" >&2
  fi
  write_statement_gate_report "ok" "statements-verified-aligned-owner-import" "aligned-owner-import" 0
else
  ALIGNED_OWNER_IMPORT_READINESS_STATUS="skipped"
fi
if [ "${BASELANE_MONTHLY_STOP_AFTER_ALIGNED_OWNER_IMPORT:-0}" = "1" ]; then
  echo "[baselane-monthly] Stopping after Aligned owner import by BASELANE_MONTHLY_STOP_AFTER_ALIGNED_OWNER_IMPORT"
  exit 0
fi

# Idempotent monthly platform expense split application + audit. Hospitable.com
# and PriceLabs charges should not remain as single 88 Madison parent rows after
# month close. The apply script is self-idempotent: it only targets unsplit,
# non-manual parent transactions in the target month. The audit is report-only;
# the cron should perform native split writes instead of failing just because
# unsplit rows were detected in the freshly synced ledger.
if [ "$DRY_RUN" = "1" ]; then
  echo "[baselane-monthly] Dry run: skipping Hospitable/PriceLabs platform split apply for ${STAMP}"
elif [ -x "$PLATFORM_SPLIT_APPLY_SCRIPT" ]; then
  echo "[baselane-monthly] Applying Hospitable/PriceLabs platform splits for ${STAMP} if needed..."
  if ! BASELANE_PLATFORM_SPLIT_YEAR="$TARGET_YEAR" BASELANE_PLATFORM_SPLIT_MONTH="$TARGET_MONTH" DRY_RUN=0 "$NODE" "$PLATFORM_SPLIT_APPLY_SCRIPT"; then
    echo "[baselane-monthly] Platform split apply failed; continuing to audit gate" >&2
  fi
  PLATFORM_APPLY_REPORT="$ROOT/reports/baselane_platform_subscription_splits_${TARGET_YEAR}_$(printf '%02d' "$TARGET_MONTH")_apply.json"
  PLATFORM_APPLIED_COUNT="$($PY - "$PLATFORM_APPLY_REPORT" <<'PY'
import json, sys
try:
    print(int(json.load(open(sys.argv[1])).get('applied') or 0))
except Exception:
    print(0)
PY
)"
  if [ "$PLATFORM_APPLIED_COUNT" != "0" ]; then
    echo "[baselane-monthly] Platform splits applied; refreshing Baselane ledger export before audit..."
    BASELANE_SYNC_SCRIPT="${BASELANE_SYNC_SCRIPT:-$ROOT/scripts/baselane_sync_native_ui.py}"
    "$PY" "$BASELANE_SYNC_SCRIPT"
  fi
else
  echo "[baselane-monthly] Platform split apply script missing or not executable, skipping apply"
fi

if [ -x "$PLATFORM_SPLIT_AUDIT_SCRIPT" ]; then
  echo "[baselane-monthly] Auditing Hospitable/PriceLabs platform splits for ${STAMP}..."
  "$PY" "$PLATFORM_SPLIT_AUDIT_SCRIPT" --year "$TARGET_YEAR" --month "$TARGET_MONTH"
else
  echo "[baselane-monthly] Platform split audit script missing or not executable, skipping"
fi

# Idempotent monthly Baselane-native split workflow. This covers only
# deterministic high-confidence shared vendors from baselane_native_split_plan.py,
# including Madison Morgan Linen and Spectrum Madison parent rows.
if [ -f "$NATIVE_SPLIT_APPLY_SCRIPT" ]; then
  echo "[baselane-monthly] Applying guarded Baselane native splits for ${STAMP} if enabled..."
  if [ -f "$NATIVE_SPLIT_PLAN_SCRIPT" ]; then
    "$PY" "$NATIVE_SPLIT_PLAN_SCRIPT" \
      --report "$ROOT/reports/baselane_native_split_plan.json" \
      --csv "$ROOT/reports/baselane_native_split_plan.csv" \
      --markdown "$ROOT/reports/baselane_native_split_plan.md" >/dev/null || true
  fi
  native_split_apply_args=(
    --plan "$ROOT/reports/baselane_native_split_plan.json"
    --report "$ROOT/reports/baselane_native_split_apply_report.json"
  )
  if [ "${BASELANE_NATIVE_SPLIT_APPLY:-0}" = "1" ]; then
    native_split_apply_args+=(--apply)
  fi
  if ! "$PY" "$NATIVE_SPLIT_APPLY_SCRIPT" "${native_split_apply_args[@]}"; then
    echo "[baselane-monthly] Native split apply returned review/failure; continuing because downstream reports surface the gate" >&2
  fi
  if [ -f "$NATIVE_SPLIT_PLAN_SCRIPT" ]; then
    "$PY" "$NATIVE_SPLIT_PLAN_SCRIPT" \
      --report "$ROOT/reports/baselane_native_split_plan.json" \
      --csv "$ROOT/reports/baselane_native_split_plan.csv" \
      --markdown "$ROOT/reports/baselane_native_split_plan.md" \
      --apply-report "$ROOT/reports/baselane_native_split_apply_report.json" >/dev/null || true
  fi
  NATIVE_SPLIT_APPLY_REPORT="$ROOT/reports/baselane_native_split_apply_report.json"
  NATIVE_SPLIT_APPLIED_COUNT="$($PY - "$NATIVE_SPLIT_APPLY_REPORT" <<'PY'
import json, sys
try:
    print(int(json.load(open(sys.argv[1])).get('applied_count') or 0))
except Exception:
    print(0)
PY
)"
  if [ "$DRY_RUN" != "1" ] && [ "$NATIVE_SPLIT_APPLIED_COUNT" != "0" ]; then
    echo "[baselane-monthly] Native splits applied; refreshing Baselane ledger export..."
    BASELANE_SYNC_SCRIPT="${BASELANE_SYNC_SCRIPT:-$ROOT/scripts/baselane_sync_cdp_deterministic.py}"
    BASELANE_NATIVE_SPLIT_APPLY=0 "$PY" "$BASELANE_SYNC_SCRIPT"
  fi
else
  echo "[baselane-monthly] Native split apply script missing, skipping"
fi

# Idempotent monthly Obie/OSC property matching. This is report-only, no Baselane writes.
mkdir -p "$(dirname "$OBIE_STATE_FILE")"
OBIE_LAST=""
if [ -f "$OBIE_STATE_FILE" ]; then
  OBIE_LAST="$(cat "$OBIE_STATE_FILE" 2>/dev/null || true)"
fi

if [ "$OBIE_LAST" = "$STAMP" ]; then
  echo "[baselane-monthly] Obie property match for ${STAMP} already generated, skipping"
elif [ -x "$OBIE_MATCH_SCRIPT" ]; then
  echo "[baselane-monthly] Running Obie property match for ${STAMP}..."
  if "$PY" "$OBIE_MATCH_SCRIPT" --year "$TARGET_YEAR" --month "$TARGET_MONTH"; then
    if [ "$DRY_RUN" != "1" ]; then
      echo "$STAMP" > "$OBIE_STATE_FILE"
    fi
    echo "[baselane-monthly] Obie property match for ${STAMP} generated successfully"
  else
    code=$?
    echo "[baselane-monthly] Obie property match failed (exit $code), will retry next run" >&2
    exit "$code"
  fi
else
  echo "[baselane-monthly] Obie match script missing or not executable, skipping"
fi

# Idempotent monthly Obie/OSC property tagging. This performs Baselane writes only for
# high-confidence OSC - RISK SECURE rows with blank property fields. The tagger is
# self-idempotent too: already-tagged rows no longer match its blank-property preflight.
mkdir -p "$(dirname "$OBIE_TAG_STATE_FILE")"
OBIE_TAG_LAST=""
if [ -f "$OBIE_TAG_STATE_FILE" ]; then
  OBIE_TAG_LAST="$(cat "$OBIE_TAG_STATE_FILE" 2>/dev/null || true)"
fi

if [ "$OBIE_TAG_LAST" = "$STAMP" ]; then
  echo "[baselane-monthly] Obie property tagging for ${STAMP} already applied, skipping"
elif [ "$DRY_RUN" = "1" ]; then
  echo "[baselane-monthly] Dry run: skipping Obie property tagging for ${STAMP}"
elif [ -x "$OBIE_TAG_SCRIPT" ]; then
  echo "[baselane-monthly] Running Obie property tagging for ${STAMP}..."
  if DRY_RUN=0 "$NODE" "$OBIE_TAG_SCRIPT"; then
    echo "$STAMP" > "$OBIE_TAG_STATE_FILE"
    echo "[baselane-monthly] Obie property tagging for ${STAMP} applied successfully"
  else
    code=$?
    echo "[baselane-monthly] Obie property tagging failed (exit $code), will retry next run" >&2
    exit "$code"
  fi
else
  echo "[baselane-monthly] Obie tag script missing or not executable, skipping"
fi
# Legacy direct Co-Ownership Mortgage Tokenomics workbook refresh. The guarded
# mortgage workflow above owns normal tokenomics writes so downloader gaps,
# HAR replayability, and split safety are evaluated before workbook mutation.
if [ "$BASELANE_LEGACY_DIRECT_TOKENOMICS_UPDATE" = "1" ]; then
  TOKENOMICS_SCRIPT="${COOWNERSHIP_MORTGAGE_TOKENOMICS_SCRIPT:-$ROOT/scripts/update_coownership_mortgage_tokenomics.py}"
  if [ -x "$TOKENOMICS_SCRIPT" ]; then
    if "$PY" -c 'import openpyxl, pypdf' >/dev/null 2>&1; then
      echo "[baselane-monthly] Updating Co-Ownership Mortgage Tokenomics workbook via legacy direct updater..."
      "$PY" "$TOKENOMICS_SCRIPT"
    else
      echo "[baselane-monthly] Skipping Co-Ownership Mortgage Tokenomics update: Python modules openpyxl/pypdf are not installed" >&2
    fi
  else
    echo "[baselane-monthly] Tokenomics updater missing or not executable, skipping"
  fi
else
  echo "[baselane-monthly] Co-Ownership Mortgage Tokenomics workbook is handled by guarded mortgage workflow; skipping legacy direct updater"
fi
