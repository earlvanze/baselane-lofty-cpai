#!/usr/bin/env bash
set -euo pipefail

# Weekly Baselane unprocessed bookkeeping pass (Friday cadence controlled by baselane_cron_run.sh)
#
# This hook now runs a deterministic report pass so we can:
# 1) Build the unprocessed candidate queue for weekly scopes
# 2) Verify idempotency (same input -> same output signature)
# 3) Flag duplicate-transaction risk for operator review
#
# NOTE: This hook is still non-destructive (no Baselane mutations).
# It prepares queue/report artifacts for API/UI execution workflows.

OPENCLAW_ROOT="${OPENCLAW_ROOT:-$HOME/.openclaw}"
ROOT="${WORKSPACE_ROOT:-$OPENCLAW_ROOT/workspace}"
PY="${PYTHON_BIN:-python3}"
case "$PY" in
  "/usr/bin/env python3"|"env python3") PY="python3" ;;
esac
export OPENCLAW_ROOT ROOT WORKSPACE_ROOT="$ROOT"

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
export BASELANE_LEDGER_PATH="${BASELANE_LEDGER_PATH:-$BASELANE_LEDGER_DIR/ECO Systems General Ledger.csv}"
export RAW_BASELANE_LEDGER_PATH="$BASELANE_LEDGER_PATH"

SCOPE_GUARD_SCRIPT="$ROOT/scripts/baselane_scope_guard.py"
SCOPE_GUARD_FILE="$ROOT/reports/baselane_weekly_unprocessed_scope_guard.json"
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
    echo "[$(date -Is)] weekly pass refused invalid financial source path; see $SCOPE_GUARD_FILE" >&2
    exit "$scope_guard_rc"
  fi
fi

REPORT_SCRIPT="$ROOT/scripts/baselane_weekly_unprocessed_report.py"
REPORT_JSON="$ROOT/reports/baselane_weekly_unprocessed_report.json"
REPORT_CSV="$ROOT/reports/baselane_weekly_unprocessed_candidates.csv"
RAW_DUPLICATE_REPORT_JSON="$ROOT/reports/baselane_weekly_raw_duplicate_report.json"
RAW_DUPLICATE_REPORT_CSV="$ROOT/reports/baselane_weekly_raw_duplicate_candidates.csv"
RAW_DUPLICATE_STATE_FILE="$ROOT/scripts/.baselane_weekly_raw_duplicate_state.json"
BASELANE_REPORTING_LEDGER_PATH="${BASELANE_REPORTING_LEDGER_PATH:-$ROOT/reports/baselane_weekly_deduped_reporting_ledger.csv}"
BASELANE_CLEAN_REPORTING_LEDGER_PATH="${BASELANE_CLEAN_REPORTING_LEDGER_PATH:-$ROOT/reports/baselane_weekly_clean_reporting_ledger.csv}"
BASELANE_SAFE_CATEGORY_LEDGER_PATH="${BASELANE_SAFE_CATEGORY_LEDGER_PATH:-$ROOT/reports/baselane_weekly_safe_category_reporting_ledger.csv}"
BASELANE_NO_DAO_MORTGAGE_CLEAN_LEDGER_PATH="${BASELANE_NO_DAO_MORTGAGE_CLEAN_LEDGER_PATH:-$ROOT/reports/baselane_weekly_no_dao_mortgage_clean_reporting_ledger.csv}"
BASELANE_CF_REPORTING_LEDGER_PATH="${BASELANE_CF_REPORTING_LEDGER_PATH:-$BASELANE_NO_DAO_MORTGAGE_CLEAN_LEDGER_PATH}"
ECOGL_SAFE_APPLY_SCRIPT="$ROOT/scripts/baselane_ecogl_apply_safe_actions.py"
ECOGL_SAFE_APPLY_REPORT="$ROOT/reports/baselane_ecogl_safe_category_apply_report.json"
ECOGL_SAFE_APPLY_ACTIONS_CSV="$ROOT/reports/baselane_ecogl_safe_category_apply_actions.csv"
ECOGL_SAFE_APPLY_MD="$ROOT/reports/baselane_ecogl_safe_category_apply_report.md"
FIRST_DAY_PM_FEE_QUARANTINE_SCRIPT="$ROOT/scripts/baselane_quarantine_first_day_pm_fee_rows.py"
FIRST_DAY_PM_FEE_QUARANTINE_REPORT="$ROOT/reports/baselane_first_day_pm_fee_quarantine_report.json"
FIRST_DAY_PM_FEE_QUARANTINE_CSV="$ROOT/reports/baselane_first_day_pm_fee_quarantine_rows.csv"
FIRST_DAY_PM_FEE_QUARANTINE_MD="$ROOT/reports/baselane_first_day_pm_fee_quarantine_report.md"
NO_DAO_MORTGAGE_QUARANTINE_SCRIPT="$ROOT/scripts/baselane_quarantine_no_dao_mortgage_rows.py"
NO_DAO_MORTGAGE_QUARANTINE_REPORT="$ROOT/reports/baselane_no_dao_mortgage_reporting_quarantine.json"
NO_DAO_MORTGAGE_QUARANTINE_CSV="$ROOT/reports/baselane_no_dao_mortgage_reporting_quarantine_rows.csv"
NO_DAO_MORTGAGE_QUARANTINE_MD="$ROOT/reports/baselane_no_dao_mortgage_reporting_quarantine.md"
SOURCE_CLEANUP_QUEUE_SCRIPT="$ROOT/scripts/baselane_source_cleanup_queue.py"
SOURCE_CLEANUP_QUEUE_REPORT="$ROOT/reports/baselane_source_cleanup_queue.json"
SOURCE_CLEANUP_QUEUE_CSV="$ROOT/reports/baselane_source_cleanup_queue.csv"
SOURCE_CLEANUP_QUEUE_MD="$ROOT/reports/baselane_source_cleanup_queue.md"
ECOGL_SOURCE_FIX_SCRIPT="$ROOT/scripts/baselane_ecogl_source_fix_plan.py"
ECOGL_SOURCE_FIX_PLAN="$ROOT/reports/baselane_ecogl_source_fix_plan.json"
ECOGL_SOURCE_FIX_ACTIONS="$ROOT/reports/baselane_ecogl_source_fix_actions.csv"
ECOGL_SOURCE_FIX_MD="$ROOT/reports/baselane_ecogl_source_fix_plan.md"
ECOGL_ACCRUAL_OVERLAY_SCRIPT="$ROOT/scripts/baselane_ecogl_apply_accrual_overlay.py"
BASELANE_ACCRUAL_OVERLAY_LEDGER_PATH="$ROOT/reports/baselane_weekly_accrual_overlay_reporting_ledger.csv"
ECOGL_ACCRUAL_OVERLAY_REPORT="$ROOT/reports/baselane_ecogl_accrual_overlay_report.json"
ECOGL_ACCRUAL_OVERLAY_ACTIONS="$ROOT/reports/baselane_ecogl_accrual_overlay_actions.csv"
ECOGL_ACCRUAL_OVERLAY_MD="$ROOT/reports/baselane_ecogl_accrual_overlay_report.md"
TMP_JSON="$ROOT/reports/.baselane_weekly_unprocessed_report.verify.json"
TMP_CSV="$ROOT/reports/.baselane_weekly_unprocessed_candidates.verify.csv"
STATE_FILE="$ROOT/scripts/.baselane_weekly_unprocessed_state.json"
DUPLICATE_ALLOWLIST="$ROOT/scripts/.baselane_weekly_duplicate_allowlist.json"
ALERT_FILE="$ROOT/reports/baselane_weekly_alerts.txt"
CF_MONTH="${CF_MONTH:-$($PY - <<'PY'
from datetime import date, timedelta
today = date.today()
first_of_month = today.replace(day=1)
prior_closed_month = first_of_month - timedelta(days=1)
print(f"{prior_closed_month.year:04d}-{prior_closed_month.month:02d}")
PY
)}"

# Weekly reconciliation is report-first. External or financial writes require a
# deliberate run-level approval in addition to their individual opt-in flags.
BASELANE_WEEKLY_LIVE_ACTIONS_APPROVED="${BASELANE_WEEKLY_LIVE_ACTIONS_APPROVED:-0}"
CF_SYNC_MODE_ARGS=()
if [ "$BASELANE_WEEKLY_LIVE_ACTIONS_APPROVED" != "1" ]; then
  export YHOME_GSHEET_APPLY=0
  export CF_BALANCE_SHEET_CASH_APPLY=0
  export CF_BALANCE_SHEET_CREATE_MISSING_ROWS=0
  export BASELANE_NATIVE_SPLIT_APPLY=0
  CF_SYNC_MODE_ARGS=(--audit)
fi

if [ ! -f "$REPORT_SCRIPT" ]; then
  echo "[$(date -Is)] weekly pass failed: missing $REPORT_SCRIPT" >&2
  exit 1
fi

# Raw export pass: preserve source duplicate diagnostics, but write an exact-row
# deduped reporting ledger for downstream weekly/monthly financial artifacts.
$PY "$REPORT_SCRIPT" \
  --ledger "$RAW_BASELANE_LEDGER_PATH" \
  --out-json "$RAW_DUPLICATE_REPORT_JSON" \
  --out-csv "$RAW_DUPLICATE_REPORT_CSV" \
  --state-file "$RAW_DUPLICATE_STATE_FILE" \
  --duplicate-allowlist "$DUPLICATE_ALLOWLIST" \
  --deduped-ledger-out "$BASELANE_REPORTING_LEDGER_PATH"

if [ ! -f "$ECOGL_SAFE_APPLY_SCRIPT" ]; then
  echo "[$(date -Is)] weekly pass failed: missing $ECOGL_SAFE_APPLY_SCRIPT" >&2
  exit 1
fi

# Apply deterministic high-confidence category fixes only to the derived
# reporting ledger. The raw Baselane export remains untouched.
$PY "$ECOGL_SAFE_APPLY_SCRIPT" \
  --ledger "$BASELANE_REPORTING_LEDGER_PATH" \
  --out-ledger "$BASELANE_SAFE_CATEGORY_LEDGER_PATH" \
  --report "$ECOGL_SAFE_APPLY_REPORT" \
  --actions-csv "$ECOGL_SAFE_APPLY_ACTIONS_CSV" \
  --markdown "$ECOGL_SAFE_APPLY_MD" \
  --historical-apply-month "$CF_MONTH" \
  --apply

if [ ! -f "$FIRST_DAY_PM_FEE_QUARANTINE_SCRIPT" ]; then
  echo "[$(date -Is)] weekly pass failed: missing $FIRST_DAY_PM_FEE_QUARANTINE_SCRIPT" >&2
  exit 1
fi

# Remove the legacy 1st-day PM-fee accrual lane from derived reporting only.
# Raw Baselane/ECO GL remains unchanged for source-cleanup evidence.
$PY "$FIRST_DAY_PM_FEE_QUARANTINE_SCRIPT" \
  --ledger "$BASELANE_SAFE_CATEGORY_LEDGER_PATH" \
  --out-ledger "$BASELANE_CLEAN_REPORTING_LEDGER_PATH" \
  --report "$FIRST_DAY_PM_FEE_QUARANTINE_REPORT" \
  --quarantine-csv "$FIRST_DAY_PM_FEE_QUARANTINE_CSV" \
  --markdown "$FIRST_DAY_PM_FEE_QUARANTINE_MD" \
  --all-months

if [ ! -f "$NO_DAO_MORTGAGE_QUARANTINE_SCRIPT" ]; then
  echo "[$(date -Is)] weekly pass failed: missing $NO_DAO_MORTGAGE_QUARANTINE_SCRIPT" >&2
  exit 1
fi

# Remove mortgage/debt rows for properties where the DAO has no mortgage from
# derived reporting only. The raw Baselane/ECO GL remains untouched for source
# cleanup evidence; downstream CF/source-cash must not inherit the bad rows.
$PY "$NO_DAO_MORTGAGE_QUARANTINE_SCRIPT" \
  --ledger "$BASELANE_CLEAN_REPORTING_LEDGER_PATH" \
  --out-ledger "$BASELANE_NO_DAO_MORTGAGE_CLEAN_LEDGER_PATH" \
  --report "$NO_DAO_MORTGAGE_QUARANTINE_REPORT" \
  --quarantine-csv "$NO_DAO_MORTGAGE_QUARANTINE_CSV" \
  --markdown "$NO_DAO_MORTGAGE_QUARANTINE_MD"

if [ -f "$SOURCE_CLEANUP_QUEUE_SCRIPT" ]; then
  $PY "$SOURCE_CLEANUP_QUEUE_SCRIPT" \
    --ledger "$RAW_BASELANE_LEDGER_PATH" \
    --report "$SOURCE_CLEANUP_QUEUE_REPORT" \
    --csv "$SOURCE_CLEANUP_QUEUE_CSV" \
    --markdown "$SOURCE_CLEANUP_QUEUE_MD" >/dev/null || true
fi

# Pass 1: operate on the exact-row-deduped, safe-category-cleaned, no-DAO
# mortgage-clean reporting ledger.
$PY "$REPORT_SCRIPT" --ledger "$BASELANE_CF_REPORTING_LEDGER_PATH" --out-json "$REPORT_JSON" --out-csv "$REPORT_CSV" --state-file "$STATE_FILE" --duplicate-allowlist "$DUPLICATE_ALLOWLIST"

# Pass 2 (verification pass for idempotency)
$PY "$REPORT_SCRIPT" --ledger "$BASELANE_CF_REPORTING_LEDGER_PATH" --out-json "$TMP_JSON" --out-csv "$TMP_CSV" --state-file "$STATE_FILE" --duplicate-allowlist "$DUPLICATE_ALLOWLIST"

HASH1="$(sha256sum "$REPORT_CSV" | awk '{print $1}')"
HASH2="$(sha256sum "$TMP_CSV" | awk '{print $1}')"

if [ "$HASH1" != "$HASH2" ]; then
  echo "[$(date -Is)] weekly pass idempotency check FAILED: candidate hashes differ" >&2
  exit 2
fi

# Read duplicate count and candidate count
read -r DUP_COUNT PENDING_DUP_COUNT BLOCKING_DUP_COUNT CAND_PENDING_DUP_COUNT CAND_COUNT IDEMPOTENT RAW_DUP_COUNT RAW_EXACT_EXTRA_COUNT REPORTING_REMOVED_COUNT <<EOF
$($PY - <<'PY'
import json
import os
from pathlib import Path
root = Path(os.environ['ROOT'])
p = root / 'reports' / 'baselane_weekly_unprocessed_report.json'
d = json.loads(p.read_text())
raw = json.loads((root / 'reports' / 'baselane_weekly_raw_duplicate_report.json').read_text())
print(
    d.get('duplicate_key_count', 0),
    d.get('duplicate_review_pending_count', d.get('duplicate_key_count', 0)),
    d.get('duplicate_review_blocking_count', 0),
    d.get('candidate_duplicate_pending_count', d.get('candidate_duplicate_key_count', 0)),
    d.get('candidate_count', 0),
    d.get('idempotency', {}).get('idempotent', False),
    raw.get('duplicate_key_count', 0),
    raw.get('exact_duplicate_extra_row_count', 0),
    raw.get('deduped_reporting_ledger_removed_row_count', 0),
)
PY
)
EOF

rm -f "$TMP_JSON" "$TMP_CSV"

{
  echo "[$(date -Is)] baselane_weekly_unprocessed_pass"
  echo "candidate_count=$CAND_COUNT"
  echo "raw_duplicate_key_count=$RAW_DUP_COUNT"
  echo "raw_exact_duplicate_extra_row_count=$RAW_EXACT_EXTRA_COUNT"
  echo "deduped_reporting_ledger_removed_row_count=$REPORTING_REMOVED_COUNT"
  echo "reporting_duplicate_key_count=$DUP_COUNT"
  echo "reporting_duplicate_review_pending_count=$PENDING_DUP_COUNT"
  echo "reporting_duplicate_review_blocking_count=$BLOCKING_DUP_COUNT"
  echo "candidate_duplicate_pending_count=$CAND_PENDING_DUP_COUNT"
  echo "idempotent=$IDEMPOTENT"
  echo "raw_duplicate_report_json=$RAW_DUPLICATE_REPORT_JSON"
  echo "reporting_ledger=$BASELANE_REPORTING_LEDGER_PATH"
  echo "safe_category_ledger=$BASELANE_SAFE_CATEGORY_LEDGER_PATH"
  echo "clean_reporting_ledger=$BASELANE_CLEAN_REPORTING_LEDGER_PATH"
  echo "no_dao_mortgage_clean_reporting_ledger=$BASELANE_NO_DAO_MORTGAGE_CLEAN_LEDGER_PATH"
  echo "cf_reporting_ledger=$BASELANE_CF_REPORTING_LEDGER_PATH"
  echo "ecogl_safe_apply_report=$ECOGL_SAFE_APPLY_REPORT"
  echo "first_day_pm_fee_quarantine_report=$FIRST_DAY_PM_FEE_QUARANTINE_REPORT"
  echo "no_dao_mortgage_quarantine_report=$NO_DAO_MORTGAGE_QUARANTINE_REPORT"
  echo "report_json=$REPORT_JSON"
  echo "report_csv=$REPORT_CSV"
  echo "duplicate_allowlist=$DUPLICATE_ALLOWLIST"
} >> "$ALERT_FILE"

if [ "$BLOCKING_DUP_COUNT" != "0" ]; then
  echo "[$(date -Is)] weekly pass warning: blocking duplicate transaction keys detected after exact-row dedupe ($BLOCKING_DUP_COUNT). See $ALERT_FILE" >&2
fi

# ── Weekly CF Statement Sync ──────────────────────────────────────────────────
# Update per-property Cash Flow xlsx files from Baselane GL data.
# Idempotent: overwrites only plain-value cells, preserves formulas.
# Flags conflicts (> $0.01 delta) to alerts channel.
CF_SYNC_SCRIPT="$ROOT/skills/baselane-financials/scripts/update_cf_statements.py"
UNTAGGED_REVIEW_SCRIPT="$ROOT/scripts/baselane_cf_untagged_review_packet.py"
UNTAGGED_RULE_CANDIDATES_SCRIPT="$ROOT/scripts/baselane_cf_untagged_rule_candidates.py"
CONFLICT_REVIEW_SCRIPT="$ROOT/scripts/baselane_cf_conflict_review_packet.py"
CONFLICT_RESOLUTION_SCRIPT="$ROOT/scripts/baselane_cf_conflict_resolution_plan.py"
CONFLICT_AUTO_APPROVAL_SCRIPT="$ROOT/scripts/baselane_cf_auto_approve_zero_fill.py"
CF_SYNC_LOG="$ROOT/reports/cf_statement_sync.log"
CF_SYNC_REPORT="$ROOT/reports/baselane_weekly_cf_statement_sync_report.json"
CF_SYNC_TIMEOUT_SECONDS="${CF_SYNC_TIMEOUT_SECONDS:-900}"
UNTAGGED_REVIEW_JSON="$ROOT/reports/baselane_cf_untagged_review_packet.json"
UNTAGGED_REVIEW_CSV="$ROOT/reports/baselane_cf_untagged_review_packet.csv"
UNTAGGED_REVIEW_MD="$ROOT/reports/baselane_cf_untagged_review_packet.md"
UNTAGGED_RULE_CANDIDATES_JSON="$ROOT/reports/baselane_cf_untagged_rule_candidates.json"
UNTAGGED_RULE_CANDIDATES_CSV="$ROOT/reports/baselane_cf_untagged_rule_candidates.csv"
UNTAGGED_RULE_CANDIDATES_MD="$ROOT/reports/baselane_cf_untagged_rule_candidates.md"
LISTING_UPDATE_POLICY_JSON="$ROOT/config/lofty_listing_update_policy.json"
CONFLICT_REVIEW_JSON="$ROOT/reports/baselane_cf_conflict_review_packet.json"
CONFLICT_REVIEW_CSV="$ROOT/reports/baselane_cf_conflict_review_packet.csv"
CONFLICT_REVIEW_MD="$ROOT/reports/baselane_cf_conflict_review_packet.md"
CONFLICT_RESOLUTION_APPROVAL_TEMPLATE="$ROOT/reports/baselane_cf_conflict_resolution_approval_template.json"
VERIFIED_DUPLICATE_PM_VOIDS_JSON="$ROOT/config/baselane_cf_verified_duplicate_pm_voids.json"
SOURCE_TRANSACTION_INDEX="$ROOT/reports/baselane_source_transaction_index.csv"
CONFLICT_AUTO_APPROVAL_JSON="$ROOT/reports/baselane_cf_conflict_auto_zero_fill_approval.json"
CONFLICT_AUTO_APPROVAL_REPORT="$ROOT/reports/baselane_cf_conflict_auto_zero_fill_report.json"
CONFLICT_AUTO_APPROVAL_MD="$ROOT/reports/baselane_cf_conflict_auto_zero_fill_report.md"
CONFLICT_AUTO_APPLY_REPORT="$ROOT/reports/baselane_cf_conflict_auto_zero_fill_apply_report.json"
CONFLICT_AUTO_APPLY_MD="$ROOT/reports/baselane_cf_conflict_auto_zero_fill_apply_report.md"
CONFLICT_RESOLUTION_REPORT="$ROOT/reports/baselane_cf_conflict_resolution_plan.json"
CONFLICT_RESOLUTION_MD="$ROOT/reports/baselane_cf_conflict_resolution_plan.md"
CF_BALANCE_SHEET_AUDIT_SCRIPT="$ROOT/scripts/baselane_cf_balance_sheet_consistency_audit.py"
CF_BALANCE_SHEET_AUDIT_REPORT="$ROOT/reports/baselane_cf_balance_sheet_consistency_audit.json"
CF_BALANCE_SHEET_CASH_APPLY_SCRIPT="$ROOT/scripts/baselane_cf_balance_sheet_cash_apply.py"
CF_BALANCE_SHEET_CASH_APPLY_REPORT="$ROOT/reports/baselane_cf_balance_sheet_cash_apply_report.json"
YHOME_TRANSITION_RECONCILIATION_URL="${YHOME_TRANSITION_RECONCILIATION_URL:-https://docs.google.com/spreadsheets/d/1HerPv9U7IB47ipCpJ-XshajQWouCUEwfDdkHSfVCwfc/export?format=csv&gid=1187056671}"
YHOME_TRANSITION_RECONCILIATION_CSV="${YHOME_TRANSITION_RECONCILIATION_CSV:-$ROOT/reports/yhome_transition_reconciliation.csv}"
YHOME_GWS_SPREADSHEET_ID="${YHOME_GWS_SPREADSHEET_ID:-1HerPv9U7IB47ipCpJ-XshajQWouCUEwfDdkHSfVCwfc}"
YHOME_GWS_SHEET_TITLE="${YHOME_GWS_SHEET_TITLE:-Cleveland}"
YHOME_GWS_SHEET_SPECS="${YHOME_GWS_SHEET_SPECS:-Cleveland=1187056671|Chicago & non-Yhome=433920866|Yhome Deeded & Sold=1902489452}"
YHOME_TRANSITION_RECONCILIATION_SNAPSHOT_SCRIPT="$ROOT/scripts/refresh_yhome_transition_reconciliation.py"
YHOME_TRANSITION_RECONCILIATION_SNAPSHOT_REPORT="$ROOT/reports/yhome_transition_reconciliation_snapshot.json"
export YHOME_GWS_SPREADSHEET_ID YHOME_GWS_SHEET_TITLE YHOME_GWS_SHEET_SPECS
YHOME_OPERATING_CASH_UPDATE_PLAN_CSV="$ROOT/reports/yhome_operating_cash_update_plan.csv"
YHOME_OPERATING_CASH_GSHEET_UPDATER_SCRIPT="$ROOT/scripts/yhome_operating_cash_gsheet_update.py"
YHOME_OPERATING_CASH_GSHEET_UPDATE_REPORT="$ROOT/reports/yhome_operating_cash_gsheet_update_report.json"
YHOME_OPERATING_CASH_APPLY_VERIFY_SCRIPT="$ROOT/scripts/yhome_operating_cash_apply_verify.py"
YHOME_OPERATING_CASH_APPLY_VERIFY_REPORT="$ROOT/reports/yhome_operating_cash_apply_verify_report.json"
LOFTY_TRANSFER_REQUIREMENTS_SCRIPT="$ROOT/scripts/baselane_lofty_transfer_requirements.py"
LOFTY_TRANSFER_REQUIREMENTS_REPORT="$ROOT/reports/baselane_lofty_transfer_requirements.json"
LOFTY_TRANSFER_REQUIREMENTS_CSV="$ROOT/reports/baselane_lofty_transfer_requirements.csv"
LOFTY_TRANSFER_REQUIREMENTS_MD="$ROOT/reports/baselane_lofty_transfer_requirements.md"
LOFTY_TRANSFER_REQUIREMENTS_TELEGRAM_MD="$ROOT/reports/baselane_lofty_transfer_requirements.telegram.md"

refresh_yhome_transition_reconciliation_csv() {
  mkdir -p "$(dirname "$YHOME_TRANSITION_RECONCILIATION_CSV")"
  # refresh_yhome_transition_reconciliation.py performs the read-only
  # `gws sheets spreadsheets values batchGet` with
  # {"valueRenderOption": "UNFORMATTED_VALUE"}.
  if command -v gws >/dev/null 2>&1 && [ -n "$YHOME_GWS_SPREADSHEET_ID" ] && [ -f "$YHOME_TRANSITION_RECONCILIATION_SNAPSHOT_SCRIPT" ]; then
    local snapshot_args=(
      --spreadsheet-id "$YHOME_GWS_SPREADSHEET_ID"
      --output "$YHOME_TRANSITION_RECONCILIATION_CSV"
      --report "$YHOME_TRANSITION_RECONCILIATION_SNAPSHOT_REPORT"
    )
    local sheet_spec
    IFS='|' read -ra sheet_specs <<< "$YHOME_GWS_SHEET_SPECS"
    for sheet_spec in "${sheet_specs[@]}"; do
      snapshot_args+=(--sheet "$sheet_spec")
    done
    if "$PY" "$YHOME_TRANSITION_RECONCILIATION_SNAPSHOT_SCRIPT" "${snapshot_args[@]}" 2>&1 | tee -a "$CF_SYNC_LOG"; then
      return 0
    fi
    echo "[$(date -Is)] multi-tab Yhome snapshot failed; preserving the last snapshot" >&2
    return 1
  fi
  if [ -z "$YHOME_TRANSITION_RECONCILIATION_URL" ]; then
    return 0
  fi
  curl -L --fail --silent --show-error "$YHOME_TRANSITION_RECONCILIATION_URL" -o "$YHOME_TRANSITION_RECONCILIATION_CSV"
}

run_cf_balance_sheet_consistency_audit() {
  if [ ! -f "$CF_BALANCE_SHEET_AUDIT_SCRIPT" ]; then
    return 0
  fi
  if [ -n "$YHOME_TRANSITION_RECONCILIATION_URL" ]; then
    refresh_yhome_transition_reconciliation_csv 2>&1 | tee -a "$CF_SYNC_LOG" || true
  fi
  set +e
  "$PY" "$CF_BALANCE_SHEET_AUDIT_SCRIPT" \
    --month "$CF_MONTH" \
    --candidate-packet "$ROOT/reports/baselane_financials_monthly_review_candidate_packet.json" \
    --yhome-csv "$YHOME_TRANSITION_RECONCILIATION_CSV" \
    --report "$CF_BALANCE_SHEET_AUDIT_REPORT" \
    --yhome-plan-csv "$YHOME_OPERATING_CASH_UPDATE_PLAN_CSV" \
    --audit-workbooks \
    --workbook-timeout-seconds "${CF_BALANCE_SHEET_WORKBOOK_TIMEOUT_SECONDS:-10}" \
    2>&1 | tee -a "$CF_SYNC_LOG"
  local audit_rc="${PIPESTATUS[0]}"
  set -e
  return "$audit_rc"
}

run_yhome_operating_cash_gsheet_update_report() {
  if [ ! -f "$YHOME_OPERATING_CASH_GSHEET_UPDATER_SCRIPT" ]; then
    return 0
  fi
  local apply_arg=()
  if [ "${YHOME_GSHEET_APPLY:-0}" = "1" ]; then
    apply_arg=(--apply)
  fi
  set +e
  "$PY" "$YHOME_OPERATING_CASH_GSHEET_UPDATER_SCRIPT" \
    --plan-csv "$YHOME_OPERATING_CASH_UPDATE_PLAN_CSV" \
    --yhome-csv "$YHOME_TRANSITION_RECONCILIATION_CSV" \
    --report "$YHOME_OPERATING_CASH_GSHEET_UPDATE_REPORT" \
    "${apply_arg[@]}" \
    2>&1 | tee -a "$CF_SYNC_LOG"
  local updater_rc="${PIPESTATUS[0]}"
  set -e
  return "$updater_rc"
}

yhome_operating_cash_applied_update_count() {
  if [ ! -f "$YHOME_OPERATING_CASH_GSHEET_UPDATE_REPORT" ]; then
    echo 0
    return 0
  fi
  YHOME_OPERATING_CASH_GSHEET_UPDATE_REPORT="$YHOME_OPERATING_CASH_GSHEET_UPDATE_REPORT" "$PY" - <<'PY'
import json
import os
from pathlib import Path

path = Path(os.environ["YHOME_OPERATING_CASH_GSHEET_UPDATE_REPORT"])
try:
    report = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    print(0)
else:
    print(int(report.get("applied_update_count") or 0))
PY
}

run_yhome_operating_cash_apply_verify_report() {
  if [ ! -f "$YHOME_OPERATING_CASH_APPLY_VERIFY_SCRIPT" ]; then
    run_cf_balance_sheet_consistency_audit || true
    run_yhome_operating_cash_gsheet_update_report || true
    return 0
  fi
  local apply_arg=()
  if [ "${YHOME_GSHEET_APPLY:-0}" = "1" ]; then
    apply_arg=(--apply)
  fi
  set +e
  "$PY" "$YHOME_OPERATING_CASH_APPLY_VERIFY_SCRIPT" \
    --month "$CF_MONTH" \
    --candidate-packet "$ROOT/reports/baselane_financials_monthly_review_candidate_packet.json" \
    --yhome-csv "$YHOME_TRANSITION_RECONCILIATION_CSV" \
    --audit-script "$CF_BALANCE_SHEET_AUDIT_SCRIPT" \
    --audit-report "$CF_BALANCE_SHEET_AUDIT_REPORT" \
    --plan-csv "$YHOME_OPERATING_CASH_UPDATE_PLAN_CSV" \
	    --updater-script "$YHOME_OPERATING_CASH_GSHEET_UPDATER_SCRIPT" \
	    --updater-report "$YHOME_OPERATING_CASH_GSHEET_UPDATE_REPORT" \
	    --report "$YHOME_OPERATING_CASH_APPLY_VERIFY_REPORT" \
	    --audit-workbooks \
	    --workbook-timeout-seconds "${CF_BALANCE_SHEET_WORKBOOK_TIMEOUT_SECONDS:-10}" \
	    "${apply_arg[@]}" \
    2>&1 | tee -a "$CF_SYNC_LOG"
  local wrapper_rc="${PIPESTATUS[0]}"
  set -e
  return "$wrapper_rc"
}

run_cf_balance_sheet_cash_apply_report() {
  if [ ! -f "$CF_BALANCE_SHEET_CASH_APPLY_SCRIPT" ]; then
    return 0
  fi
  local apply_arg=()
  if [ "${CF_BALANCE_SHEET_CASH_APPLY:-0}" = "1" ]; then
    apply_arg+=(--apply)
  fi
  if [ "${CF_BALANCE_SHEET_CREATE_MISSING_ROWS:-0}" = "1" ]; then
    apply_arg+=(--create-missing-rows)
  fi
  set +e
  "$PY" "$CF_BALANCE_SHEET_CASH_APPLY_SCRIPT" \
    --month "$CF_MONTH" \
    --candidate-packet "$ROOT/reports/baselane_financials_monthly_review_candidate_packet.json" \
    --source-cleanup-queue "$SOURCE_CLEANUP_QUEUE_REPORT" \
    --source-cash-report "$ROOT/reports/baselane_daily_source_cash_balance_report.json" \
    --report "$CF_BALANCE_SHEET_CASH_APPLY_REPORT" \
    "${apply_arg[@]}" \
    2>&1 | tee -a "$CF_SYNC_LOG"
  local cash_apply_rc="${PIPESTATUS[0]}"
  set -e
  return "$cash_apply_rc"
}

run_lofty_transfer_requirements_report() {
  if [ ! -f "$LOFTY_TRANSFER_REQUIREMENTS_SCRIPT" ]; then
    return 0
  fi
  set +e
  "$PY" "$LOFTY_TRANSFER_REQUIREMENTS_SCRIPT" \
    --candidate-packet "$ROOT/reports/baselane_financials_monthly_review_candidate_packet.json" \
    --cf-balance-sheet-report "$CF_BALANCE_SHEET_AUDIT_REPORT" \
    --source-cleanup-queue "$SOURCE_CLEANUP_QUEUE_REPORT" \
    --source-cash-report "$ROOT/reports/baselane_daily_source_cash_balance_report.json" \
    --yhome-csv "$YHOME_TRANSITION_RECONCILIATION_CSV" \
    --yhome-update-plan-csv "$YHOME_OPERATING_CASH_UPDATE_PLAN_CSV" \
    --report "$LOFTY_TRANSFER_REQUIREMENTS_REPORT" \
    --csv "$LOFTY_TRANSFER_REQUIREMENTS_CSV" \
    --markdown "$LOFTY_TRANSFER_REQUIREMENTS_MD" \
    --telegram-markdown "$LOFTY_TRANSFER_REQUIREMENTS_TELEGRAM_MD" \
    2>&1 | tee -a "$CF_SYNC_LOG"
  local transfer_rc="${PIPESTATUS[0]}"
  set -e
  return "$transfer_rc"
}

write_cf_sync_report() {
  local status="$1"
  local rc="$2"
  local reason="${3:-}"
  run_cf_balance_sheet_cash_apply_report || true
  run_yhome_operating_cash_apply_verify_report || true
  run_cf_balance_sheet_consistency_audit || true
  run_lofty_transfer_requirements_report || true
  CF_SYNC_REPORT="$CF_SYNC_REPORT" \
  CF_SYNC_STATUS="$status" \
  CF_SYNC_RC="$rc" \
  CF_SYNC_REASON="$reason" \
  CF_SYNC_SCRIPT="$CF_SYNC_SCRIPT" \
  UNTAGGED_REVIEW_SCRIPT="$UNTAGGED_REVIEW_SCRIPT" \
  UNTAGGED_RULE_CANDIDATES_SCRIPT="$UNTAGGED_RULE_CANDIDATES_SCRIPT" \
  CONFLICT_REVIEW_SCRIPT="$CONFLICT_REVIEW_SCRIPT" \
  CONFLICT_RESOLUTION_SCRIPT="$CONFLICT_RESOLUTION_SCRIPT" \
  CONFLICT_AUTO_APPROVAL_SCRIPT="$CONFLICT_AUTO_APPROVAL_SCRIPT" \
  UNTAGGED_REVIEW_JSON="$UNTAGGED_REVIEW_JSON" \
  UNTAGGED_REVIEW_CSV="$UNTAGGED_REVIEW_CSV" \
  UNTAGGED_REVIEW_MD="$UNTAGGED_REVIEW_MD" \
  UNTAGGED_RULE_CANDIDATES_JSON="$UNTAGGED_RULE_CANDIDATES_JSON" \
  UNTAGGED_RULE_CANDIDATES_CSV="$UNTAGGED_RULE_CANDIDATES_CSV" \
  UNTAGGED_RULE_CANDIDATES_MD="$UNTAGGED_RULE_CANDIDATES_MD" \
  CONFLICT_REVIEW_JSON="$CONFLICT_REVIEW_JSON" \
  CONFLICT_REVIEW_CSV="$CONFLICT_REVIEW_CSV" \
  CONFLICT_REVIEW_MD="$CONFLICT_REVIEW_MD" \
  CONFLICT_RESOLUTION_APPROVAL_TEMPLATE="$CONFLICT_RESOLUTION_APPROVAL_TEMPLATE" \
  CONFLICT_AUTO_APPROVAL_JSON="$CONFLICT_AUTO_APPROVAL_JSON" \
  CONFLICT_AUTO_APPROVAL_REPORT="$CONFLICT_AUTO_APPROVAL_REPORT" \
  CONFLICT_AUTO_APPROVAL_MD="$CONFLICT_AUTO_APPROVAL_MD" \
  CONFLICT_AUTO_APPLY_REPORT="$CONFLICT_AUTO_APPLY_REPORT" \
  CONFLICT_AUTO_APPLY_MD="$CONFLICT_AUTO_APPLY_MD" \
  CONFLICT_RESOLUTION_REPORT="$CONFLICT_RESOLUTION_REPORT" \
  CONFLICT_RESOLUTION_MD="$CONFLICT_RESOLUTION_MD" \
  CF_BALANCE_SHEET_AUDIT_SCRIPT="$CF_BALANCE_SHEET_AUDIT_SCRIPT" \
  CF_BALANCE_SHEET_AUDIT_REPORT="$CF_BALANCE_SHEET_AUDIT_REPORT" \
  CF_BALANCE_SHEET_CASH_APPLY_SCRIPT="$CF_BALANCE_SHEET_CASH_APPLY_SCRIPT" \
  CF_BALANCE_SHEET_CASH_APPLY_REPORT="$CF_BALANCE_SHEET_CASH_APPLY_REPORT" \
  YHOME_TRANSITION_RECONCILIATION_CSV="$YHOME_TRANSITION_RECONCILIATION_CSV" \
  YHOME_OPERATING_CASH_UPDATE_PLAN_CSV="$YHOME_OPERATING_CASH_UPDATE_PLAN_CSV" \
  YHOME_OPERATING_CASH_GSHEET_UPDATER_SCRIPT="$YHOME_OPERATING_CASH_GSHEET_UPDATER_SCRIPT" \
  YHOME_OPERATING_CASH_GSHEET_UPDATE_REPORT="$YHOME_OPERATING_CASH_GSHEET_UPDATE_REPORT" \
  YHOME_OPERATING_CASH_APPLY_VERIFY_SCRIPT="$YHOME_OPERATING_CASH_APPLY_VERIFY_SCRIPT" \
  YHOME_OPERATING_CASH_APPLY_VERIFY_REPORT="$YHOME_OPERATING_CASH_APPLY_VERIFY_REPORT" \
  LOFTY_TRANSFER_REQUIREMENTS_SCRIPT="$LOFTY_TRANSFER_REQUIREMENTS_SCRIPT" \
  LOFTY_TRANSFER_REQUIREMENTS_REPORT="$LOFTY_TRANSFER_REQUIREMENTS_REPORT" \
  LOFTY_TRANSFER_REQUIREMENTS_CSV="$LOFTY_TRANSFER_REQUIREMENTS_CSV" \
  LOFTY_TRANSFER_REQUIREMENTS_MD="$LOFTY_TRANSFER_REQUIREMENTS_MD" \
  LOFTY_TRANSFER_REQUIREMENTS_TELEGRAM_MD="$LOFTY_TRANSFER_REQUIREMENTS_TELEGRAM_MD" \
  CF_SYNC_LOG="$CF_SYNC_LOG" \
  CF_SYNC_TIMEOUT_SECONDS="$CF_SYNC_TIMEOUT_SECONDS" \
  BASELANE_LEDGER_PATH="$BASELANE_CF_REPORTING_LEDGER_PATH" \
  RAW_BASELANE_LEDGER_PATH="$RAW_BASELANE_LEDGER_PATH" \
  BASELANE_REPORTING_LEDGER_PATH="$BASELANE_REPORTING_LEDGER_PATH" \
  BASELANE_SAFE_CATEGORY_LEDGER_PATH="$BASELANE_SAFE_CATEGORY_LEDGER_PATH" \
  BASELANE_CLEAN_REPORTING_LEDGER_PATH="$BASELANE_CLEAN_REPORTING_LEDGER_PATH" \
  BASELANE_CF_REPORTING_LEDGER_PATH="$BASELANE_CF_REPORTING_LEDGER_PATH" \
  BASELANE_ACCRUAL_OVERLAY_LEDGER_PATH="$BASELANE_ACCRUAL_OVERLAY_LEDGER_PATH" \
  ECOGL_SAFE_APPLY_REPORT="$ECOGL_SAFE_APPLY_REPORT" \
  ECOGL_SAFE_APPLY_ACTIONS_CSV="$ECOGL_SAFE_APPLY_ACTIONS_CSV" \
  ECOGL_SAFE_APPLY_MD="$ECOGL_SAFE_APPLY_MD" \
  FIRST_DAY_PM_FEE_QUARANTINE_REPORT="$FIRST_DAY_PM_FEE_QUARANTINE_REPORT" \
  FIRST_DAY_PM_FEE_QUARANTINE_CSV="$FIRST_DAY_PM_FEE_QUARANTINE_CSV" \
  FIRST_DAY_PM_FEE_QUARANTINE_MD="$FIRST_DAY_PM_FEE_QUARANTINE_MD" \
  NO_DAO_MORTGAGE_QUARANTINE_REPORT="$NO_DAO_MORTGAGE_QUARANTINE_REPORT" \
  NO_DAO_MORTGAGE_QUARANTINE_CSV="$NO_DAO_MORTGAGE_QUARANTINE_CSV" \
  NO_DAO_MORTGAGE_QUARANTINE_MD="$NO_DAO_MORTGAGE_QUARANTINE_MD" \
  SOURCE_CLEANUP_QUEUE_REPORT="$SOURCE_CLEANUP_QUEUE_REPORT" \
  SOURCE_CLEANUP_QUEUE_CSV="$SOURCE_CLEANUP_QUEUE_CSV" \
  SOURCE_CLEANUP_QUEUE_MD="$SOURCE_CLEANUP_QUEUE_MD" \
  ECOGL_SOURCE_FIX_PLAN="$ECOGL_SOURCE_FIX_PLAN" \
  ECOGL_SOURCE_FIX_ACTIONS="$ECOGL_SOURCE_FIX_ACTIONS" \
  ECOGL_SOURCE_FIX_MD="$ECOGL_SOURCE_FIX_MD" \
  ECOGL_ACCRUAL_OVERLAY_REPORT="$ECOGL_ACCRUAL_OVERLAY_REPORT" \
  ECOGL_ACCRUAL_OVERLAY_ACTIONS="$ECOGL_ACCRUAL_OVERLAY_ACTIONS" \
  ECOGL_ACCRUAL_OVERLAY_MD="$ECOGL_ACCRUAL_OVERLAY_MD" \
  DROPBOX_ROOT="$DROPBOX_ROOT" \
  ROOT="$ROOT" \
  $PY - <<'PY'
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

def cf_review_packet(report, rows):
    conflict_rows = [row for row in rows if isinstance(row, dict) and row.get("conflicts")]
    error_rows = [row for row in rows if isinstance(row, dict) and row.get("error")]
    untagged_rows = [
        row for row in rows
        if isinstance(row, dict) and int((row.get("summary") or {}).get("untagged_gl_rows") or 0) > 0
    ]
    lines = [
        "# Baselane Weekly CF Statement Review",
        "",
        "Purpose: review CF statement audit issues before relying on weekly/monthly financial reporting.",
        "",
        "## Summary",
        f"- Status: {report.get('status')}",
        f"- Reason: {report.get('reason')}",
        f"- Audit report: `{report.get('audit_report')}`",
        f"- Audit errors: {report.get('audit_error_count', 0)}",
        f"- Audit error classes: {report.get('audit_error_class_counts') or {}}",
        f"- Conflicts: {report.get('conflict_count', 0)} across {report.get('conflict_property_count', 0)} properties",
        f"- Untagged GL rows using keyword fallback: {report.get('untagged_gl_rows', 0)}",
        f"- IL/OH/TN mortgage/debt invariant violations: {report.get('no_mortgage_debt_violation_count', 0)}",
        f"- Excluded sold/closed/manual CF folders: {report.get('excluded_cf_property_count', 0)}",
        f"- Ignored legacy/noncanonical CF candidates: {report.get('ignored_cf_candidate_count', 0)}",
        f"- Missing canonical CF workbooks: {report.get('missing_canonical_cf_count', 0)}",
        "",
        "## Required Review Order",
        "1. Fix audit errors first; missing month columns/sheets mean those workbooks are not auditable for the run month.",
        "2. Review conflicts where CF has values but GL is empty; these usually indicate missing Baselane accrual/tagging or stale CF rows.",
        "3. Reduce untagged GL rows by tagging Baselane category/sub-category instead of relying on keyword fallback.",
        "",
        "## Audit Errors",
    ]
    if error_rows:
        for index, row in enumerate(error_rows[:40], start=1):
            summary = row.get("summary") or {}
            lines.extend(
                [
                    f"- {index}. {summary.get('property') or 'Unknown property'} — {row.get('error')}",
                    f"  - File: `{summary.get('file') or 'unknown'}`",
                ]
            )
    else:
        lines.append("- None.")
    lines.extend(["", "## Conflict Properties"])
    if conflict_rows:
        for index, row in enumerate(conflict_rows[:30], start=1):
            summary = row.get("summary") or {}
            lines.append(f"- {index}. {summary.get('property') or 'Unknown property'} — {len(row.get('conflicts') or [])} conflicts")
            lines.append(f"  - File: `{summary.get('file') or 'unknown'}`")
            for conflict in (row.get("conflicts") or [])[:5]:
                lines.append(
                    "  - "
                    f"{conflict.get('label')}: CF={conflict.get('cf_value')} "
                    f"GL={conflict.get('gl_total')} diff={conflict.get('diff')} "
                    f"action={conflict.get('action') or conflict.get('type')}"
                )
    else:
        lines.append("- None.")
    lines.extend(["", "## Untagged GL Rows By Property"])
    if untagged_rows:
        for index, row in enumerate(
            sorted(untagged_rows, key=lambda item: int((item.get("summary") or {}).get("untagged_gl_rows") or 0), reverse=True)[:30],
            start=1,
        ):
            summary = row.get("summary") or {}
            lines.append(f"- {index}. {summary.get('property') or 'Unknown property'} — {summary.get('untagged_gl_rows')} untagged rows")
    else:
        lines.append("- None.")
    lines.append("")
    return "\n".join(lines)

report = {
    "job": "baselane-weekly-cf-statement-sync",
    "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "status": os.environ["CF_SYNC_STATUS"],
    "return_code": int(os.environ["CF_SYNC_RC"]),
    "reason": os.environ.get("CF_SYNC_REASON") or None,
    "script": os.environ["CF_SYNC_SCRIPT"],
    "script_exists": Path(os.environ["CF_SYNC_SCRIPT"]).is_file(),
    "untagged_review_script": os.environ["UNTAGGED_REVIEW_SCRIPT"],
    "untagged_review_script_exists": Path(os.environ["UNTAGGED_REVIEW_SCRIPT"]).is_file(),
    "untagged_rule_candidates_script": os.environ["UNTAGGED_RULE_CANDIDATES_SCRIPT"],
    "untagged_rule_candidates_script_exists": Path(os.environ["UNTAGGED_RULE_CANDIDATES_SCRIPT"]).is_file(),
    "conflict_review_script": os.environ["CONFLICT_REVIEW_SCRIPT"],
    "conflict_review_script_exists": Path(os.environ["CONFLICT_REVIEW_SCRIPT"]).is_file(),
    "conflict_resolution_script": os.environ["CONFLICT_RESOLUTION_SCRIPT"],
    "conflict_resolution_script_exists": Path(os.environ["CONFLICT_RESOLUTION_SCRIPT"]).is_file(),
    "conflict_auto_approval_script": os.environ["CONFLICT_AUTO_APPROVAL_SCRIPT"],
    "conflict_auto_approval_script_exists": Path(os.environ["CONFLICT_AUTO_APPROVAL_SCRIPT"]).is_file(),
    "untagged_review_packet": os.environ["UNTAGGED_REVIEW_JSON"],
    "untagged_review_csv": os.environ["UNTAGGED_REVIEW_CSV"],
    "untagged_review_markdown": os.environ["UNTAGGED_REVIEW_MD"],
    "untagged_rule_candidates_packet": os.environ["UNTAGGED_RULE_CANDIDATES_JSON"],
    "untagged_rule_candidates_csv": os.environ["UNTAGGED_RULE_CANDIDATES_CSV"],
    "untagged_rule_candidates_markdown": os.environ["UNTAGGED_RULE_CANDIDATES_MD"],
    "conflict_review_packet": os.environ["CONFLICT_REVIEW_JSON"],
    "conflict_review_csv": os.environ["CONFLICT_REVIEW_CSV"],
    "conflict_review_markdown": os.environ["CONFLICT_REVIEW_MD"],
    "conflict_resolution_approval_template": os.environ["CONFLICT_RESOLUTION_APPROVAL_TEMPLATE"],
    "conflict_auto_approval_json": os.environ["CONFLICT_AUTO_APPROVAL_JSON"],
    "conflict_auto_approval_report": os.environ["CONFLICT_AUTO_APPROVAL_REPORT"],
    "conflict_auto_approval_markdown": os.environ["CONFLICT_AUTO_APPROVAL_MD"],
    "conflict_auto_apply_report": os.environ["CONFLICT_AUTO_APPLY_REPORT"],
    "conflict_auto_apply_markdown": os.environ["CONFLICT_AUTO_APPLY_MD"],
    "conflict_resolution_plan": os.environ["CONFLICT_RESOLUTION_REPORT"],
    "conflict_resolution_markdown": os.environ["CONFLICT_RESOLUTION_MD"],
    "cf_balance_sheet_consistency_script": os.environ["CF_BALANCE_SHEET_AUDIT_SCRIPT"],
    "cf_balance_sheet_consistency_script_exists": Path(os.environ["CF_BALANCE_SHEET_AUDIT_SCRIPT"]).is_file(),
    "cf_balance_sheet_consistency_report": os.environ["CF_BALANCE_SHEET_AUDIT_REPORT"],
    "cf_balance_sheet_cash_apply_script": os.environ["CF_BALANCE_SHEET_CASH_APPLY_SCRIPT"],
    "cf_balance_sheet_cash_apply_script_exists": Path(os.environ["CF_BALANCE_SHEET_CASH_APPLY_SCRIPT"]).is_file(),
    "cf_balance_sheet_cash_apply_report": os.environ["CF_BALANCE_SHEET_CASH_APPLY_REPORT"],
    "yhome_transition_reconciliation_csv": os.environ["YHOME_TRANSITION_RECONCILIATION_CSV"],
    "yhome_operating_cash_update_plan_csv": os.environ["YHOME_OPERATING_CASH_UPDATE_PLAN_CSV"],
    "yhome_operating_cash_gsheet_updater_script": os.environ["YHOME_OPERATING_CASH_GSHEET_UPDATER_SCRIPT"],
    "yhome_operating_cash_gsheet_updater_script_exists": Path(os.environ["YHOME_OPERATING_CASH_GSHEET_UPDATER_SCRIPT"]).is_file(),
    "yhome_operating_cash_gsheet_update_report": os.environ["YHOME_OPERATING_CASH_GSHEET_UPDATE_REPORT"],
    "yhome_operating_cash_apply_verify_script": os.environ["YHOME_OPERATING_CASH_APPLY_VERIFY_SCRIPT"],
    "yhome_operating_cash_apply_verify_script_exists": Path(os.environ["YHOME_OPERATING_CASH_APPLY_VERIFY_SCRIPT"]).is_file(),
    "yhome_operating_cash_apply_verify_report": os.environ["YHOME_OPERATING_CASH_APPLY_VERIFY_REPORT"],
    "lofty_transfer_requirements_script": os.environ["LOFTY_TRANSFER_REQUIREMENTS_SCRIPT"],
    "lofty_transfer_requirements_script_exists": Path(os.environ["LOFTY_TRANSFER_REQUIREMENTS_SCRIPT"]).is_file(),
    "lofty_transfer_requirements_report": os.environ["LOFTY_TRANSFER_REQUIREMENTS_REPORT"],
    "lofty_transfer_requirements_csv": os.environ["LOFTY_TRANSFER_REQUIREMENTS_CSV"],
    "lofty_transfer_requirements_markdown": os.environ["LOFTY_TRANSFER_REQUIREMENTS_MD"],
    "lofty_transfer_requirements_telegram_markdown": os.environ["LOFTY_TRANSFER_REQUIREMENTS_TELEGRAM_MD"],
    "log_file": os.environ["CF_SYNC_LOG"],
    "cf_sync_timeout_seconds": int(os.environ.get("CF_SYNC_TIMEOUT_SECONDS") or 0),
    "log_exists": Path(os.environ["CF_SYNC_LOG"]).is_file(),
    "baselane_ledger_path": os.environ["BASELANE_LEDGER_PATH"],
    "raw_baselane_ledger_path": os.environ.get("RAW_BASELANE_LEDGER_PATH"),
    "baselane_reporting_ledger_path": os.environ.get("BASELANE_REPORTING_LEDGER_PATH"),
    "baselane_safe_category_ledger_path": os.environ.get("BASELANE_SAFE_CATEGORY_LEDGER_PATH"),
    "baselane_clean_reporting_ledger_path": os.environ.get("BASELANE_CLEAN_REPORTING_LEDGER_PATH"),
    "baselane_cf_reporting_ledger_path": os.environ.get("BASELANE_CF_REPORTING_LEDGER_PATH"),
    "baselane_accrual_overlay_ledger_path": os.environ.get("BASELANE_ACCRUAL_OVERLAY_LEDGER_PATH"),
    "ecogl_safe_apply_report": os.environ.get("ECOGL_SAFE_APPLY_REPORT"),
    "ecogl_safe_apply_actions_csv": os.environ.get("ECOGL_SAFE_APPLY_ACTIONS_CSV"),
    "ecogl_safe_apply_markdown": os.environ.get("ECOGL_SAFE_APPLY_MD"),
    "first_day_pm_fee_quarantine_report": os.environ.get("FIRST_DAY_PM_FEE_QUARANTINE_REPORT"),
    "first_day_pm_fee_quarantine_csv": os.environ.get("FIRST_DAY_PM_FEE_QUARANTINE_CSV"),
    "first_day_pm_fee_quarantine_markdown": os.environ.get("FIRST_DAY_PM_FEE_QUARANTINE_MD"),
    "no_dao_mortgage_quarantine_report": os.environ.get("NO_DAO_MORTGAGE_QUARANTINE_REPORT"),
    "no_dao_mortgage_quarantine_csv": os.environ.get("NO_DAO_MORTGAGE_QUARANTINE_CSV"),
    "no_dao_mortgage_quarantine_markdown": os.environ.get("NO_DAO_MORTGAGE_QUARANTINE_MD"),
    "source_cleanup_queue_report": os.environ.get("SOURCE_CLEANUP_QUEUE_REPORT"),
    "source_cleanup_queue_csv": os.environ.get("SOURCE_CLEANUP_QUEUE_CSV"),
    "source_cleanup_queue_markdown": os.environ.get("SOURCE_CLEANUP_QUEUE_MD"),
    "ecogl_source_fix_plan": os.environ.get("ECOGL_SOURCE_FIX_PLAN"),
    "ecogl_source_fix_actions": os.environ.get("ECOGL_SOURCE_FIX_ACTIONS"),
    "ecogl_source_fix_markdown": os.environ.get("ECOGL_SOURCE_FIX_MD"),
    "ecogl_accrual_overlay_report": os.environ.get("ECOGL_ACCRUAL_OVERLAY_REPORT"),
    "ecogl_accrual_overlay_actions": os.environ.get("ECOGL_ACCRUAL_OVERLAY_ACTIONS"),
    "ecogl_accrual_overlay_markdown": os.environ.get("ECOGL_ACCRUAL_OVERLAY_MD"),
    "dropbox_root": os.environ["DROPBOX_ROOT"],
    "real_estate_root": str(Path(os.environ["DROPBOX_ROOT"]) / "Real Estate"),
}
balance_sheet_audit_path = Path(os.environ["CF_BALANCE_SHEET_AUDIT_REPORT"])
if balance_sheet_audit_path.exists():
    try:
        balance_sheet_audit = json.loads(balance_sheet_audit_path.read_text(encoding="utf-8"))
    except Exception as exc:
        report["cf_balance_sheet_consistency_status"] = "unreadable"
        report["cf_balance_sheet_consistency_error"] = str(exc)
        if report["status"] == "ok":
            report["status"] = "review"
            report["reason"] = "cf_balance_sheet_consistency_unreadable"
    else:
        report.update(
            {
                "cf_balance_sheet_consistency_status": balance_sheet_audit.get("status"),
                "cf_balance_sheet_consistency_issue_count": int(balance_sheet_audit.get("issue_count") or 0),
                "cf_balance_sheet_consistency_issue_type_counts": balance_sheet_audit.get("issue_type_counts"),
                "cf_balance_sheet_consistency_yhome_update_required_count": int(balance_sheet_audit.get("yhome_update_required_count") or 0),
                "cf_balance_sheet_consistency_yhome_skip_inactive_count": int(balance_sheet_audit.get("yhome_skip_inactive_count") or 0),
                "cf_balance_sheet_consistency_yhome_unmatched_candidate_count": int(balance_sheet_audit.get("yhome_unmatched_candidate_count") or 0),
                "cf_balance_sheet_consistency_yhome_target_columns": balance_sheet_audit.get("yhome_target_columns"),
                "cf_balance_sheet_consistency_policy": balance_sheet_audit.get("cf_balance_sheet_policy"),
                "yhome_weekly_operating_cash_policy": balance_sheet_audit.get("yhome_weekly_policy"),
            }
        )
        if balance_sheet_audit.get("status") != "ok" and report["status"] == "ok":
            report["status"] = "review"
            report["reason"] = "cf_balance_sheet_consistency_review"
else:
    report["cf_balance_sheet_consistency_status"] = "missing"
yhome_gsheet_update_path = Path(os.environ["YHOME_OPERATING_CASH_GSHEET_UPDATE_REPORT"])
if yhome_gsheet_update_path.exists():
    try:
        yhome_gsheet_update = json.loads(yhome_gsheet_update_path.read_text(encoding="utf-8"))
    except Exception as exc:
        report["yhome_operating_cash_gsheet_update_status"] = "unreadable"
        report["yhome_operating_cash_gsheet_update_error"] = str(exc)
    else:
        report.update(
            {
                "yhome_operating_cash_gsheet_update_status": yhome_gsheet_update.get("status"),
                "yhome_operating_cash_gsheet_update_reason": yhome_gsheet_update.get("reason"),
                "yhome_operating_cash_gsheet_update_count": int(yhome_gsheet_update.get("update_count") or 0),
                "yhome_operating_cash_gsheet_request_count": int(yhome_gsheet_update.get("request_count") or 0),
                "yhome_operating_cash_gsheet_applied_update_count": int(yhome_gsheet_update.get("applied_update_count") or 0),
                "yhome_operating_cash_gsheet_apply_requested": bool(yhome_gsheet_update.get("apply_requested")),
                "yhome_operating_cash_gsheet_write_enabled": bool(yhome_gsheet_update.get("write_enabled")),
                "yhome_operating_cash_gsheet_dry_run": bool(yhome_gsheet_update.get("dry_run")),
                "yhome_operating_cash_gsheet_range_mode": yhome_gsheet_update.get("range_mode"),
                "yhome_operating_cash_gsheet_idempotency_key": yhome_gsheet_update.get("idempotency_key"),
            }
        )
else:
    report["yhome_operating_cash_gsheet_update_status"] = "missing"
yhome_apply_verify_path = Path(os.environ["YHOME_OPERATING_CASH_APPLY_VERIFY_REPORT"])
if yhome_apply_verify_path.exists():
    try:
        yhome_apply_verify = json.loads(yhome_apply_verify_path.read_text(encoding="utf-8"))
    except Exception as exc:
        report["yhome_operating_cash_apply_verify_status"] = "unreadable"
        report["yhome_operating_cash_apply_verify_error"] = str(exc)
    else:
        report.update(
            {
                "yhome_operating_cash_apply_verify_status": yhome_apply_verify.get("status"),
                "yhome_operating_cash_apply_verify_reason": yhome_apply_verify.get("reason"),
                "yhome_operating_cash_apply_verify_pre_update_required_count": int(yhome_apply_verify.get("pre_yhome_update_required_count") or 0),
                "yhome_operating_cash_apply_verify_post_update_required_count": int(yhome_apply_verify.get("post_yhome_update_required_count") or 0),
                "yhome_operating_cash_apply_verify_applied_update_count": int(yhome_apply_verify.get("applied_update_count") or 0),
                "yhome_operating_cash_apply_verify_apply_requested": bool(yhome_apply_verify.get("apply_requested")),
                "yhome_operating_cash_apply_verify_write_enabled": bool(yhome_apply_verify.get("write_enabled")),
                "yhome_operating_cash_apply_verify_dry_run": bool(yhome_apply_verify.get("dry_run")),
                "yhome_operating_cash_apply_verify_external_write_attempted": bool(yhome_apply_verify.get("external_write_attempted")),
            }
        )
else:
    report["yhome_operating_cash_apply_verify_status"] = "missing"
cf_cash_apply_path = Path(os.environ["CF_BALANCE_SHEET_CASH_APPLY_REPORT"])
if cf_cash_apply_path.exists():
    try:
        cf_cash_apply = json.loads(cf_cash_apply_path.read_text(encoding="utf-8"))
    except Exception as exc:
        report["cf_balance_sheet_cash_apply_status"] = "unreadable"
        report["cf_balance_sheet_cash_apply_error"] = str(exc)
    else:
        report.update(
            {
                "cf_balance_sheet_cash_apply_status": cf_cash_apply.get("status"),
                "cf_balance_sheet_cash_apply_effective_mode": cf_cash_apply.get("effective_mode"),
                "cf_balance_sheet_cash_apply_property_count": int(cf_cash_apply.get("property_count") or 0),
                "cf_balance_sheet_cash_apply_change_count": int(cf_cash_apply.get("change_count") or 0),
                "cf_balance_sheet_cash_apply_action_counts": cf_cash_apply.get("action_counts"),
                "cf_balance_sheet_cash_apply_blocked_by_source_guard": bool(cf_cash_apply.get("apply_blocked_by_source_guard")),
                "cf_balance_sheet_cash_apply_report": str(cf_cash_apply_path),
            }
        )
else:
    report["cf_balance_sheet_cash_apply_status"] = "missing"
lofty_transfer_path = Path(os.environ["LOFTY_TRANSFER_REQUIREMENTS_REPORT"])
if lofty_transfer_path.exists():
    try:
        lofty_transfer = json.loads(lofty_transfer_path.read_text(encoding="utf-8"))
    except Exception as exc:
        report["lofty_transfer_requirements_status"] = "unreadable"
        report["lofty_transfer_requirements_error"] = str(exc)
        if report["status"] == "ok":
            report["status"] = "review"
            report["reason"] = "lofty_transfer_requirements_unreadable"
    else:
        report.update(
            {
                "lofty_transfer_requirements_status": lofty_transfer.get("status"),
                "lofty_transfer_requirements_property_count": int(lofty_transfer.get("property_count") or 0),
                "lofty_transfer_requirements_ready_to_send_property_count": int(lofty_transfer.get("ready_to_send_property_count") or 0),
                "lofty_transfer_requirements_held_property_count": int(lofty_transfer.get("held_property_count") or 0),
                "lofty_transfer_requirements_recommended_send_to_lofty_total": lofty_transfer.get("recommended_send_to_lofty_total"),
                "lofty_transfer_requirements_provisional_send_to_lofty_total": lofty_transfer.get("provisional_send_to_lofty_total"),
                "lofty_transfer_requirements_eco_cash_shortfall_total": lofty_transfer.get("eco_cash_shortfall_total"),
                "lofty_transfer_requirements_source_clean_for_final_transfer_amounts": bool(lofty_transfer.get("source_clean_for_final_transfer_amounts")),
                "lofty_transfer_requirements_action_counts": lofty_transfer.get("action_counts"),
                "lofty_transfer_requirements_report": str(lofty_transfer_path),
                "lofty_transfer_requirements_csv": os.environ["LOFTY_TRANSFER_REQUIREMENTS_CSV"],
                "lofty_transfer_requirements_markdown": os.environ["LOFTY_TRANSFER_REQUIREMENTS_MD"],
                "lofty_transfer_requirements_telegram_markdown": os.environ["LOFTY_TRANSFER_REQUIREMENTS_TELEGRAM_MD"],
            }
        )
        if lofty_transfer.get("status") != "ok" and report["status"] == "ok":
            report["status"] = "review"
            report["reason"] = "lofty_transfer_requirements_review"
else:
    report["lofty_transfer_requirements_status"] = "missing"
safe_apply_path = Path(os.environ.get("ECOGL_SAFE_APPLY_REPORT") or "")
if safe_apply_path.exists():
    try:
        safe_apply = json.loads(safe_apply_path.read_text(encoding="utf-8"))
        report["ecogl_safe_apply_status"] = safe_apply.get("status")
        report["ecogl_safe_apply_mode"] = safe_apply.get("mode")
        report["ecogl_safe_apply_action_count"] = safe_apply.get("safe_action_count")
        report["ecogl_safe_apply_output_written"] = safe_apply.get("output_written")
        report["ecogl_safe_apply_actions_digest"] = safe_apply.get("actions_digest")
        report["ecogl_safe_apply_output_digest"] = safe_apply.get("output_digest")
    except Exception as exc:
        report["ecogl_safe_apply_status"] = "unreadable"
        report["ecogl_safe_apply_error"] = str(exc)
quarantine_path = Path(os.environ.get("FIRST_DAY_PM_FEE_QUARANTINE_REPORT") or "")
if quarantine_path.exists():
    try:
        quarantine = json.loads(quarantine_path.read_text(encoding="utf-8"))
        report["first_day_pm_fee_quarantine_status"] = quarantine.get("status")
        report["first_day_pm_fee_quarantine_count"] = quarantine.get("quarantined_row_count")
        report["first_day_pm_fee_quarantine_remaining_count"] = quarantine.get("remaining_first_day_pm_fee_count")
        report["first_day_pm_fee_quarantine_reporting_output_clean"] = quarantine.get("reporting_output_clean")
        report["first_day_pm_fee_quarantine_digest"] = quarantine.get("quarantine_digest")
        report["first_day_pm_fee_quarantine_output_digest"] = quarantine.get("output_digest")
    except Exception as exc:
        report["first_day_pm_fee_quarantine_status"] = "unreadable"
        report["first_day_pm_fee_quarantine_error"] = str(exc)
no_dao_quarantine_path = Path(os.environ.get("NO_DAO_MORTGAGE_QUARANTINE_REPORT") or "")
if no_dao_quarantine_path.exists():
    try:
        no_dao_quarantine = json.loads(no_dao_quarantine_path.read_text(encoding="utf-8"))
        report["no_dao_mortgage_quarantine_status"] = no_dao_quarantine.get("status")
        report["no_dao_mortgage_quarantine_count"] = no_dao_quarantine.get("quarantined_row_count")
        report["no_dao_mortgage_quarantine_remaining_count"] = no_dao_quarantine.get("remaining_no_dao_mortgage_row_count")
        report["no_dao_mortgage_quarantine_reporting_output_clean"] = no_dao_quarantine.get("reporting_output_clean")
        report["no_dao_mortgage_quarantine_raw_source_mutated"] = no_dao_quarantine.get("raw_source_mutated")
        report["no_dao_mortgage_quarantine_property_counts"] = no_dao_quarantine.get("property_counts")
        report["no_dao_mortgage_quarantine_digest"] = no_dao_quarantine.get("quarantine_digest")
        report["no_dao_mortgage_quarantine_output_digest"] = no_dao_quarantine.get("output_digest")
    except Exception as exc:
        report["no_dao_mortgage_quarantine_status"] = "unreadable"
        report["no_dao_mortgage_quarantine_error"] = str(exc)
source_cleanup_queue_path = Path(os.environ.get("SOURCE_CLEANUP_QUEUE_REPORT") or "")
if source_cleanup_queue_path.exists():
    try:
        source_cleanup_queue = json.loads(source_cleanup_queue_path.read_text(encoding="utf-8"))
        report["source_cleanup_queue_status"] = source_cleanup_queue.get("status")
        report["source_cleanup_queue_action_count"] = source_cleanup_queue.get("action_count")
        report["source_cleanup_queue_missing_id_count"] = source_cleanup_queue.get("missing_id_count")
        report["source_cleanup_queue_action_counts"] = source_cleanup_queue.get("action_counts")
        report["source_cleanup_queue_live_mutation_attempted"] = source_cleanup_queue.get("live_mutation_attempted")
    except Exception as exc:
        report["source_cleanup_queue_status"] = "unreadable"
        report["source_cleanup_queue_error"] = str(exc)
source_fix_path = Path(os.environ.get("ECOGL_SOURCE_FIX_PLAN") or "")
if source_fix_path.exists():
    try:
        source_fix = json.loads(source_fix_path.read_text(encoding="utf-8"))
        report["ecogl_source_fix_status"] = source_fix.get("status")
        report["ecogl_source_fix_action_count"] = source_fix.get("action_count")
        report["ecogl_source_fix_action_type_counts"] = source_fix.get("action_type_counts")
        report["ecogl_source_fix_digest"] = source_fix.get("idempotency_digest")
    except Exception as exc:
        report["ecogl_source_fix_status"] = "unreadable"
        report["ecogl_source_fix_error"] = str(exc)
overlay_path = Path(os.environ.get("ECOGL_ACCRUAL_OVERLAY_REPORT") or "")
if overlay_path.exists():
    try:
        overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
        report["ecogl_accrual_overlay_status"] = overlay.get("status")
        report["ecogl_accrual_overlay_eligible_action_count"] = overlay.get("eligible_action_count")
        report["ecogl_accrual_overlay_appended_action_count"] = overlay.get("appended_action_count")
        report["ecogl_accrual_overlay_already_present_action_count"] = overlay.get("already_present_action_count")
        report["ecogl_accrual_overlay_actions_digest"] = overlay.get("actions_digest")
        report["ecogl_accrual_overlay_output_digest"] = overlay.get("output_digest")
    except Exception as exc:
        report["ecogl_accrual_overlay_status"] = "unreadable"
        report["ecogl_accrual_overlay_error"] = str(exc)
auto_approval_path = Path(os.environ.get("CONFLICT_AUTO_APPROVAL_REPORT") or "")
if auto_approval_path.exists():
    try:
        auto_approval = json.loads(auto_approval_path.read_text(encoding="utf-8"))
        report["conflict_auto_approval_status"] = auto_approval.get("status")
        report["conflict_auto_approval_count"] = auto_approval.get("auto_approved_count")
        report["conflict_auto_approval_excluded_applicable_count"] = auto_approval.get("excluded_applicable_count")
        report["conflict_auto_approval_blocked_count"] = auto_approval.get("blocked_count")
        report["conflict_auto_approval_digest"] = auto_approval.get("approval_digest")
    except Exception as exc:
        report["conflict_auto_approval_status"] = "unreadable"
        report["conflict_auto_approval_error"] = str(exc)
auto_apply_path = Path(os.environ.get("CONFLICT_AUTO_APPLY_REPORT") or "")
if auto_apply_path.exists():
    try:
        auto_apply = json.loads(auto_apply_path.read_text(encoding="utf-8"))
        report["conflict_auto_apply_status"] = auto_apply.get("status")
        report["conflict_auto_apply_mode"] = auto_apply.get("mode")
        report["conflict_auto_apply_status_counts"] = auto_apply.get("status_counts")
        report["conflict_auto_apply_approved_applicable_count"] = auto_apply.get("approved_applicable_count")
    except Exception as exc:
        report["conflict_auto_apply_status"] = "unreadable"
        report["conflict_auto_apply_error"] = str(exc)
untagged_review_path = Path(os.environ["UNTAGGED_REVIEW_JSON"])
if untagged_review_path.exists():
    try:
        untagged_review = json.loads(untagged_review_path.read_text(encoding="utf-8"))
        report["untagged_review_status"] = untagged_review.get("status")
        report["untagged_review_row_count"] = untagged_review.get("untagged_row_count")
        report["untagged_review_auto_suggested_count"] = untagged_review.get("auto_suggested_count")
        report["untagged_review_required_count"] = untagged_review.get("effective_review_required_count", untagged_review.get("review_required_count"))
        report["untagged_review_raw_row_count"] = untagged_review.get("untagged_row_count")
        report["untagged_review_raw_required_count"] = untagged_review.get("review_required_count")
        report["untagged_review_effective_row_count"] = untagged_review.get("effective_untagged_row_count", untagged_review.get("untagged_row_count"))
        report["untagged_review_effective_required_count"] = untagged_review.get("effective_review_required_count", untagged_review.get("review_required_count"))
    except Exception as exc:
        report["untagged_review_status"] = "unreadable"
        report["untagged_review_error"] = str(exc)
untagged_rule_candidates_path = Path(os.environ["UNTAGGED_RULE_CANDIDATES_JSON"])
if untagged_rule_candidates_path.exists():
    try:
        untagged_rule_candidates = json.loads(untagged_rule_candidates_path.read_text(encoding="utf-8"))
        report["untagged_rule_candidates_status"] = untagged_rule_candidates.get("status")
        report["untagged_rule_candidate_count"] = untagged_rule_candidates.get("candidate_count")
        report["untagged_rule_high_confidence_count"] = untagged_rule_candidates.get("high_confidence_count")
        report["untagged_rule_medium_confidence_count"] = untagged_rule_candidates.get("medium_confidence_count")
        report["untagged_rule_covered_row_count"] = untagged_rule_candidates.get("covered_row_count")
    except Exception as exc:
        report["untagged_rule_candidates_status"] = "unreadable"
        report["untagged_rule_candidates_error"] = str(exc)
conflict_review_path = Path(os.environ["CONFLICT_REVIEW_JSON"])
if conflict_review_path.exists():
    try:
        conflict_review = json.loads(conflict_review_path.read_text(encoding="utf-8"))
        report["conflict_review_status"] = conflict_review.get("status")
        report["conflict_review_conflict_count"] = conflict_review.get("conflict_count")
        report["conflict_review_high_count"] = conflict_review.get("high_count")
        report["conflict_review_medium_count"] = conflict_review.get("medium_count")
        report["conflict_review_property_count"] = conflict_review.get("property_count")
        report["conflict_review_action_counts"] = conflict_review.get("action_counts")
    except Exception as exc:
        report["conflict_review_status"] = "unreadable"
        report["conflict_review_error"] = str(exc)
conflict_resolution_path = Path(os.environ["CONFLICT_RESOLUTION_REPORT"])
if conflict_resolution_path.exists():
    try:
        conflict_resolution = json.loads(conflict_resolution_path.read_text(encoding="utf-8"))
        report["conflict_resolution_status"] = conflict_resolution.get("status")
        report["conflict_resolution_mode"] = conflict_resolution.get("mode")
        report["conflict_resolution_applicable_count"] = conflict_resolution.get("applicable_count")
        report["conflict_resolution_blocked_count"] = conflict_resolution.get("blocked_count")
        report["conflict_resolution_approved_applicable_count"] = conflict_resolution.get("approved_applicable_count")
        report["conflict_resolution_status_counts"] = conflict_resolution.get("status_counts")
    except Exception as exc:
        report["conflict_resolution_status"] = "unreadable"
        report["conflict_resolution_error"] = str(exc)
audit_dir = Path(os.environ["ROOT"]) / "reports" / "cf_statement_sync"
audit_files = sorted(audit_dir.glob("audit_*.json"), key=lambda path: path.stat().st_mtime if path.exists() else 0)
latest_audit = audit_files[-1] if audit_files else None
discovery_files = sorted(audit_dir.glob("discovery_*.json"), key=lambda path: path.stat().st_mtime if path.exists() else 0)
latest_discovery = discovery_files[-1] if discovery_files else None
source_cash_report_path = Path(os.environ["ROOT"]) / "reports" / "baselane_daily_source_cash_balance_report.json"
source_cash_report = {}
if source_cash_report_path.exists():
    try:
        loaded_source_cash_report = json.loads(source_cash_report_path.read_text(encoding="utf-8"))
        if isinstance(loaded_source_cash_report, dict):
            source_cash_report = loaded_source_cash_report
    except Exception as exc:  # noqa: BLE001
        report["source_cash_balance_report_status"] = "unreadable"
        report["source_cash_balance_report_error"] = str(exc)
if latest_discovery:
    report["discovery_report"] = str(latest_discovery)
    try:
        discovery_data = json.loads(latest_discovery.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        report["discovery_status"] = "unreadable"
        report["discovery_error"] = str(exc)
        report["status"] = "review"
        report["reason"] = report.get("reason") or "cf_discovery_unreadable"
    else:
        missing_canonical_cf_count = (
            discovery_data.get("missing_canonical_total_count")
            if discovery_data.get("missing_canonical_total_count") is not None
            else discovery_data.get("missing_canonical_from_ignored_count")
        )
        missing_canonical_cf_properties = (
            discovery_data.get("missing_canonical_total_properties")
            if discovery_data.get("missing_canonical_total_properties") is not None
            else discovery_data.get("missing_canonical_from_ignored_properties")
        )
        report.update(
            {
                "discovery_status": discovery_data.get("status"),
                "canonical_cf_property_count": discovery_data.get("canonical_property_count"),
                "canonical_owner_statement_dir_count": discovery_data.get("canonical_owner_statement_dir_count"),
                "excluded_cf_property_count": discovery_data.get("excluded_property_count"),
                "excluded_cf_properties": discovery_data.get("excluded_properties"),
                "ignored_cf_candidate_count": discovery_data.get("ignored_candidate_count"),
                "ignored_legacy_cf_candidate_count": discovery_data.get("ignored_legacy_candidate_count"),
                "ignored_noncanonical_cf_candidate_count": discovery_data.get("ignored_noncanonical_candidate_count"),
                "missing_canonical_cf_count": missing_canonical_cf_count,
                "missing_canonical_cf_properties": missing_canonical_cf_properties,
                "missing_canonical_from_ignored_count": discovery_data.get("missing_canonical_from_ignored_count"),
                "missing_canonical_from_owner_statement_dir_count": discovery_data.get("missing_canonical_from_owner_statement_dir_count"),
                "ignored_cf_candidates_by_reason": discovery_data.get("skipped_by_reason"),
            }
        )
        if int(missing_canonical_cf_count or 0) and report["status"] == "ok":
            report["status"] = "review"
            report["reason"] = "cf_discovery_missing_canonical"
if latest_audit:
    report["audit_report"] = str(latest_audit)
    try:
        audit_data = json.loads(latest_audit.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        report["audit_status"] = "unreadable"
        report["audit_error"] = str(exc)
        report["status"] = "review"
        report["reason"] = report.get("reason") or "cf_audit_unreadable"
    else:
        rows = audit_data if isinstance(audit_data, list) else []
        error_rows = [row for row in rows if isinstance(row, dict) and row.get("error")]
        conflict_rows = [row for row in rows if isinstance(row, dict) and row.get("conflicts")]
        summaries = [row.get("summary") or {} for row in rows if isinstance(row, dict)]
        conflict_count = sum(int(summary.get("conflicts") or 0) for summary in summaries)
        if not conflict_count:
            conflict_count = sum(len(row.get("conflicts") or []) for row in conflict_rows)
        untagged_gl_rows = sum(int(summary.get("untagged_gl_rows") or 0) for summary in summaries)
        formula_count = sum(int(summary.get("formulas") or 0) for summary in summaries)
        balance_sheet_skipped = sum(int(summary.get("balance_sheet_skipped") or 0) for summary in summaries)
        no_mortgage_debt_violation_count = sum(int(summary.get("no_mortgage_debt_violation_count") or 0) for summary in summaries)
        no_mortgage_debt_checked_property_count = sum(1 for summary in summaries if summary.get("no_mortgage_debt_checked") is True)
        source_cash_balance_violation_count = sum(int(summary.get("source_cash_balance_violation_count") or 0) for summary in summaries)
        standalone_source_cash_violation_count = int(source_cash_report.get("violation_count") or 0)
        source_cash_balance_no_match_count = int(source_cash_report.get("no_match_count") or 0)
        source_cash_balance_split_scope_missing_property_count = int(source_cash_report.get("split_scope_missing_property_count") or 0)
        source_cash_balance_checked_property_count_standalone = int(source_cash_report.get("checked_property_count") or 0)
        source_cash_balance_split_scope_expected_property_count = int(source_cash_report.get("split_scope_expected_property_count") or 0)
        source_cash_balance_checked_property_count = sum(1 for summary in summaries if summary.get("source_cash_balance_checked") is True)
        source_cash_balance_update_count = sum(int(summary.get("source_cash_balance_update_count") or 0) for summary in summaries)
        cf_statement_update_count = sum(int(summary.get("cf_statement_update_count") or 0) for summary in summaries)
        cf_statement_zero_fill_count = sum(int(summary.get("cf_statement_zero_fill_count") or 0) for summary in summaries)
        cf_statement_overwrite_formula_update_count = 0
        cf_statement_update_properties = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            changes = row.get("cf_statement_update_changes") or []
            if not isinstance(changes, list):
                changes = []
            formula_changes = [change for change in changes if isinstance(change, dict) and change.get("action") == "overwrite_formula"]
            cf_statement_overwrite_formula_update_count += len(formula_changes)
            summary = row.get("summary") or {}
            row_update_count = int(summary.get("cf_statement_update_count") or len(changes) or 0)
            if row_update_count:
                cf_statement_update_properties.append(
                    {
                        "property": summary.get("property") or row.get("property") or "Unknown",
                        "file": summary.get("file") or row.get("file") or "Unknown",
                        "update_count": row_update_count,
                        "zero_fill_count": int(summary.get("cf_statement_zero_fill_count") or 0),
                        "overwrite_formula_count": len(formula_changes),
                    }
                )
        no_mortgage_debt_violation_properties = [
            {
                "property": summary.get("property") or "Unknown",
                "file": summary.get("file") or "Unknown",
                "state": summary.get("no_mortgage_debt_state"),
                "violation_count": summary.get("no_mortgage_debt_violation_count"),
                "violations_bounded": summary.get("no_mortgage_debt_violations_bounded") or [],
            }
            for summary in summaries
            if int(summary.get("no_mortgage_debt_violation_count") or 0) > 0
        ]
        source_cash_balance_violation_properties = [
            {
                "property": summary.get("property") or "Unknown",
                "file": summary.get("file") or "Unknown",
                "expected": summary.get("source_cash_balance_expected"),
                "actual": summary.get("source_cash_balance_actual"),
                "violation_count": summary.get("source_cash_balance_violation_count"),
                "violations_bounded": summary.get("source_cash_balance_violations_bounded") or [],
            }
            for summary in summaries
            if int(summary.get("source_cash_balance_violation_count") or 0) > 0
        ]
        def error_class(error):
            text = str(error or "")
            if text.startswith("No column"):
                return "no_month_column"
            if text.startswith("No sheet"):
                return "no_year_sheet"
            if text.startswith("No matching property"):
                return "no_gl_property_match"
            if text.startswith("Canonical CF workbook missing"):
                return "missing_canonical_cf_workbook"
            if text.startswith("Canonical owner statement folder exists but has no Cash Flow Statement workbook"):
                return "missing_canonical_cf_workbook"
            if text.startswith("Cannot open"):
                return "cannot_open_workbook"
            return "other"
        audit_error_class_counts = Counter(error_class(row.get("error")) for row in error_rows)
        report.update(
            {
                "audit_status": "ok",
                "audited_property_count": len(rows),
                "audit_error_count": len(error_rows),
                "audit_error_class_counts": dict(sorted(audit_error_class_counts.items())),
                "conflict_count": conflict_count,
                "conflict_property_count": len(conflict_rows),
                "untagged_gl_rows": untagged_gl_rows,
                "formula_count": formula_count,
                "balance_sheet_skipped_count": balance_sheet_skipped,
                "no_mortgage_debt_policy": "IL/OH/TN cash-flow statements must have zero mortgage principal, mortgage interest, and mortgage principal balance cells.",
                "no_mortgage_debt_checked_property_count": no_mortgage_debt_checked_property_count,
                "no_mortgage_debt_violation_count": no_mortgage_debt_violation_count,
                "no_mortgage_debt_violation_properties": no_mortgage_debt_violation_properties[:25],
                "source_cash_balance_policy": "ECO GL Net Cash Balance must equal raw Baselane GL cumulative Amount through month-end, excluding EARLDAO interest rows only.",
                "source_cash_balance_report": str(source_cash_report_path),
                "source_cash_balance_report_status": source_cash_report.get("status") or report.get("source_cash_balance_report_status"),
                "source_cash_balance_checked_property_count": max(
                    source_cash_balance_checked_property_count,
                    source_cash_balance_checked_property_count_standalone,
                ),
                "source_cash_balance_checked_workbook_count": source_cash_report.get("checked_workbook_count"),
                "source_cash_balance_update_count": source_cash_balance_update_count,
                "source_cash_balance_violation_count": max(
                    source_cash_balance_violation_count,
                    standalone_source_cash_violation_count,
                ),
                "source_cash_balance_violation_properties": source_cash_balance_violation_properties[:25],
                "source_cash_balance_no_match_count": source_cash_balance_no_match_count,
                "source_cash_balance_no_match_properties": (source_cash_report.get("no_match_properties_bounded") or [])[:25],
                "source_cash_balance_split_scope_expected_property_count": source_cash_balance_split_scope_expected_property_count,
                "source_cash_balance_split_scope_missing_property_count": source_cash_balance_split_scope_missing_property_count,
                "source_cash_balance_split_scope_missing_properties": (source_cash_report.get("split_scope_missing_properties_bounded") or [])[:25],
                "cf_statement_update_count": cf_statement_update_count,
                "cf_statement_zero_fill_count": cf_statement_zero_fill_count,
                "cf_statement_overwrite_formula_update_count": cf_statement_overwrite_formula_update_count,
                "cf_statement_update_property_count": len(cf_statement_update_properties),
                "cf_statement_update_properties": cf_statement_update_properties[:25],
                "conflict_properties": [
                    (row.get("summary") or {}).get("property") or (row.get("summary") or {}).get("file") or row.get("property") or row.get("file") or "Unknown"
                    for row in conflict_rows[:25]
                    if isinstance(row, dict)
                ],
                "audit_errors_bounded": [
                    {
                        "property": (row.get("summary") or {}).get("property") or row.get("property") or "Unknown",
                        "file": (row.get("summary") or {}).get("file") or row.get("file") or "Unknown",
                        "error": row.get("error"),
                    }
                    for row in error_rows[:25]
                    if isinstance(row, dict)
                ],
            }
        )
        packet_path = Path(os.environ["ROOT"]) / "reports" / "baselane_weekly_cf_statement_review_packet.md"
        report["review_packet"] = str(packet_path)
        review_reasons = []
        if len(error_rows):
            review_reasons.append(f"audit_error_count={len(error_rows)}")
        if conflict_count:
            review_reasons.append(f"conflict_count={conflict_count}")
        if untagged_gl_rows:
            review_reasons.append(f"untagged_gl_rows={untagged_gl_rows}")
        if no_mortgage_debt_violation_count:
            review_reasons.append(f"no_mortgage_debt_violation_count={no_mortgage_debt_violation_count}")
        source_cash_balance_violation_count = int(report.get("source_cash_balance_violation_count") or 0)
        if source_cash_balance_violation_count:
            review_reasons.append(f"source_cash_balance_violation_count={source_cash_balance_violation_count}")
        if source_cash_balance_no_match_count:
            review_reasons.append(f"source_cash_balance_no_match_count={source_cash_balance_no_match_count}")
        if source_cash_balance_split_scope_missing_property_count:
            review_reasons.append(f"source_cash_balance_split_scope_missing_property_count={source_cash_balance_split_scope_missing_property_count}")
        cf_balance_sheet_issue_count = int(report.get("cf_balance_sheet_consistency_issue_count") or 0)
        if cf_balance_sheet_issue_count:
            review_reasons.append(f"cf_balance_sheet_consistency_issue_count={cf_balance_sheet_issue_count}")
        missing_canonical_cf_count = int(report.get("missing_canonical_cf_count") or 0)
        if missing_canonical_cf_count:
            review_reasons.append(f"missing_canonical_cf_count={missing_canonical_cf_count}")
        if report["status"] == "ok" and review_reasons:
            report["status"] = "review"
            report["reason"] = "cf_audit_" + "_".join(reason.split("=")[0] for reason in review_reasons)
        report["review_reasons"] = review_reasons
        packet_path.write_text(cf_review_packet(report, rows), encoding="utf-8")
else:
    report["audit_status"] = "missing"
    if report["status"] == "ok":
        report["status"] = "review"
        report["reason"] = "cf_audit_missing"
path = Path(os.environ["CF_SYNC_REPORT"])
path.parent.mkdir(parents=True, exist_ok=True)
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(path)
PY
}

if [ -f "$CF_SYNC_SCRIPT" ]; then
  echo "[$(date -Is)] Running CF statement sync..." >&2
  if [ -f "$UNTAGGED_REVIEW_SCRIPT" ]; then
    UNTAGGED_REVIEW_ARGS=(
      --gl-csv "$BASELANE_CF_REPORTING_LEDGER_PATH" \
      --month "$CF_MONTH" \
      --json "$UNTAGGED_REVIEW_JSON" \
      --csv "$UNTAGGED_REVIEW_CSV" \
      --markdown "$UNTAGGED_REVIEW_MD"
    )
    if [ -f "$ROOT/reports/baselane_source_transaction_index.csv" ]; then
      UNTAGGED_REVIEW_ARGS+=(--source-index "$ROOT/reports/baselane_source_transaction_index.csv")
    fi
    "$PY" "$UNTAGGED_REVIEW_SCRIPT" "${UNTAGGED_REVIEW_ARGS[@]}" 2>&1 | tee -a "$CF_SYNC_LOG"
  fi
  if [ -f "$UNTAGGED_RULE_CANDIDATES_SCRIPT" ] && [ -f "$UNTAGGED_REVIEW_JSON" ]; then
    UNTAGGED_RULE_ARGS=(
      --packet "$UNTAGGED_REVIEW_JSON"
      --json "$UNTAGGED_RULE_CANDIDATES_JSON"
      --csv "$UNTAGGED_RULE_CANDIDATES_CSV"
      --markdown "$UNTAGGED_RULE_CANDIDATES_MD"
    )
    if [ -f "$LISTING_UPDATE_POLICY_JSON" ]; then
      UNTAGGED_RULE_ARGS+=(--listing-update-policy "$LISTING_UPDATE_POLICY_JSON")
    fi
    $PY "$UNTAGGED_RULE_CANDIDATES_SCRIPT" \
      "${UNTAGGED_RULE_ARGS[@]}" \
      2>&1 | tee -a "$CF_SYNC_LOG" || true
  fi
  set +e
  timeout --kill-after=30s "${CF_SYNC_TIMEOUT_SECONDS}s" $PY "$CF_SYNC_SCRIPT" \
    --gl-csv "$BASELANE_CF_REPORTING_LEDGER_PATH" \
    --month "$CF_MONTH" \
    "${CF_SYNC_MODE_ARGS[@]}" \
    --source-cash-gl-csv "$BASELANE_CF_REPORTING_LEDGER_PATH" \
    --skip-source-cash-balance-row \
    --output-dir "$DROPBOX_ROOT/Real Estate" \
    --conflict-threshold 0.01 \
    --alert-file "$ALERT_FILE" \
    2>&1 | tee -a "$CF_SYNC_LOG"
  cf_sync_rc="${PIPESTATUS[0]}"
  set -e
  if [ "$cf_sync_rc" -eq 0 ]; then
    latest_audit="$($PY - "$ROOT" <<'PY'
import sys
from pathlib import Path
root = Path(sys.argv[1])
files = sorted((root / "reports" / "cf_statement_sync").glob("audit_*.json"), key=lambda path: path.stat().st_mtime if path.exists() else 0)
print(files[-1] if files else "")
PY
)"
    if [ -n "$latest_audit" ] && [ -f "$CONFLICT_REVIEW_SCRIPT" ]; then
      $PY "$CONFLICT_REVIEW_SCRIPT" \
        --audit "$latest_audit" \
        --json "$CONFLICT_REVIEW_JSON" \
        --csv "$CONFLICT_REVIEW_CSV" \
        --markdown "$CONFLICT_REVIEW_MD" \
        2>&1 | tee -a "$CF_SYNC_LOG"
    fi
    if [ -f "$CONFLICT_RESOLUTION_SCRIPT" ] && [ -f "$CONFLICT_REVIEW_JSON" ]; then
      $PY "$CONFLICT_RESOLUTION_SCRIPT" \
        --packet "$CONFLICT_REVIEW_JSON" \
        --month "$CF_MONTH" \
        --verified-voids-json "$VERIFIED_DUPLICATE_PM_VOIDS_JSON" \
        --source-transaction-index "$SOURCE_TRANSACTION_INDEX" \
        --approval-template-out "$CONFLICT_RESOLUTION_APPROVAL_TEMPLATE" \
        --report-json "$CONFLICT_RESOLUTION_REPORT" \
        --report-markdown "$CONFLICT_RESOLUTION_MD" \
        2>&1 | tee -a "$CF_SYNC_LOG"
    fi
    if [ -f "$ECOGL_SOURCE_FIX_SCRIPT" ] && [ -f "$ECOGL_ACCRUAL_OVERLAY_SCRIPT" ] && [ -f "$CONFLICT_RESOLUTION_REPORT" ] && [ -f "$UNTAGGED_REVIEW_JSON" ]; then
      set +e
      $PY "$ECOGL_SOURCE_FIX_SCRIPT" \
        --root "$ROOT" \
        --report "$ECOGL_SOURCE_FIX_PLAN" \
        --actions-csv "$ECOGL_SOURCE_FIX_ACTIONS" \
        --markdown "$ECOGL_SOURCE_FIX_MD" \
        2>&1 | tee -a "$CF_SYNC_LOG"
      source_fix_rc="${PIPESTATUS[0]}"
      set -e
      if [ "$source_fix_rc" -ne 0 ] && [ "$source_fix_rc" -ne 2 ]; then
        write_cf_sync_report "failed" "$source_fix_rc" "source_fix_plan_failed"
        echo "[$(date -Is)] ECO GL source-fix plan failed rc=$source_fix_rc" >&2
        exit "$source_fix_rc"
      fi
      $PY "$ECOGL_ACCRUAL_OVERLAY_SCRIPT" \
        --ledger "$BASELANE_CF_REPORTING_LEDGER_PATH" \
        --source-fix-plan "$ECOGL_SOURCE_FIX_PLAN" \
        --out-ledger "$BASELANE_ACCRUAL_OVERLAY_LEDGER_PATH" \
        --report "$ECOGL_ACCRUAL_OVERLAY_REPORT" \
        --actions-csv "$ECOGL_ACCRUAL_OVERLAY_ACTIONS" \
        --markdown "$ECOGL_ACCRUAL_OVERLAY_MD" \
        --apply \
        2>&1 | tee -a "$CF_SYNC_LOG"
      overlay_appended_count="$($PY - "$ECOGL_ACCRUAL_OVERLAY_REPORT" <<'PY'
import json
import sys
from pathlib import Path
path = Path(sys.argv[1])
if not path.exists():
    print(0)
else:
    print(int((json.loads(path.read_text(encoding="utf-8"))).get("appended_action_count") or 0))
PY
)"
      if [ "$overlay_appended_count" != "0" ]; then
        BASELANE_CF_REPORTING_LEDGER_PATH="$BASELANE_ACCRUAL_OVERLAY_LEDGER_PATH"
        UNTAGGED_REVIEW_ARGS=(
          --gl-csv "$BASELANE_CF_REPORTING_LEDGER_PATH" \
          --month "$CF_MONTH" \
          --json "$UNTAGGED_REVIEW_JSON" \
          --csv "$UNTAGGED_REVIEW_CSV" \
          --markdown "$UNTAGGED_REVIEW_MD"
        )
        if [ -f "$ROOT/reports/baselane_source_transaction_index.csv" ]; then
          UNTAGGED_REVIEW_ARGS+=(--source-index "$ROOT/reports/baselane_source_transaction_index.csv")
        fi
        "$PY" "$UNTAGGED_REVIEW_SCRIPT" "${UNTAGGED_REVIEW_ARGS[@]}" 2>&1 | tee -a "$CF_SYNC_LOG"
        if [ -f "$UNTAGGED_RULE_CANDIDATES_SCRIPT" ]; then
          UNTAGGED_RULE_ARGS=(
            --packet "$UNTAGGED_REVIEW_JSON"
            --json "$UNTAGGED_RULE_CANDIDATES_JSON"
            --csv "$UNTAGGED_RULE_CANDIDATES_CSV"
            --markdown "$UNTAGGED_RULE_CANDIDATES_MD"
          )
          if [ -f "$LISTING_UPDATE_POLICY_JSON" ]; then
            UNTAGGED_RULE_ARGS+=(--listing-update-policy "$LISTING_UPDATE_POLICY_JSON")
          fi
          $PY "$UNTAGGED_RULE_CANDIDATES_SCRIPT" \
            "${UNTAGGED_RULE_ARGS[@]}" \
            2>&1 | tee -a "$CF_SYNC_LOG" || true
        fi
        set +e
        timeout --kill-after=30s "${CF_SYNC_TIMEOUT_SECONDS}s" $PY "$CF_SYNC_SCRIPT" \
          --gl-csv "$BASELANE_CF_REPORTING_LEDGER_PATH" \
          --month "$CF_MONTH" \
          "${CF_SYNC_MODE_ARGS[@]}" \
          --source-cash-gl-csv "$BASELANE_CF_REPORTING_LEDGER_PATH" \
          --skip-source-cash-balance-row \
          --output-dir "$DROPBOX_ROOT/Real Estate" \
          --conflict-threshold 0.01 \
          --alert-file "$ALERT_FILE" \
          2>&1 | tee -a "$CF_SYNC_LOG"
        cf_overlay_resync_rc="${PIPESTATUS[0]}"
        set -e
        if [ "$cf_overlay_resync_rc" -eq 0 ]; then
          latest_audit="$($PY - "$ROOT" <<'PY'
import sys
from pathlib import Path
root = Path(sys.argv[1])
files = sorted((root / "reports" / "cf_statement_sync").glob("audit_*.json"), key=lambda path: path.stat().st_mtime if path.exists() else 0)
print(files[-1] if files else "")
PY
)"
          if [ -n "$latest_audit" ] && [ -f "$CONFLICT_REVIEW_SCRIPT" ]; then
            $PY "$CONFLICT_REVIEW_SCRIPT" \
              --audit "$latest_audit" \
              --json "$CONFLICT_REVIEW_JSON" \
              --csv "$CONFLICT_REVIEW_CSV" \
              --markdown "$CONFLICT_REVIEW_MD" \
              2>&1 | tee -a "$CF_SYNC_LOG"
          fi
          $PY "$CONFLICT_RESOLUTION_SCRIPT" \
            --packet "$CONFLICT_REVIEW_JSON" \
            --month "$CF_MONTH" \
            --verified-voids-json "$VERIFIED_DUPLICATE_PM_VOIDS_JSON" \
            --source-transaction-index "$SOURCE_TRANSACTION_INDEX" \
            --approval-template-out "$CONFLICT_RESOLUTION_APPROVAL_TEMPLATE" \
            --report-json "$CONFLICT_RESOLUTION_REPORT" \
            --report-markdown "$CONFLICT_RESOLUTION_MD" \
            2>&1 | tee -a "$CF_SYNC_LOG"
        else
          if [ "$cf_overlay_resync_rc" -eq 124 ] || [ "$cf_overlay_resync_rc" -eq 137 ]; then
            write_cf_sync_report "failed" "$cf_overlay_resync_rc" "helper_timeout_after_accrual_overlay"
          else
            write_cf_sync_report "failed" "$cf_overlay_resync_rc" "helper_returned_nonzero_after_accrual_overlay"
          fi
          echo "[$(date -Is)] CF statement re-sync failed after accrual overlay rc=$cf_overlay_resync_rc" >&2
          exit "$cf_overlay_resync_rc"
        fi
      fi
    fi
    if [ "$BASELANE_WEEKLY_LIVE_ACTIONS_APPROVED" = "1" ] && [ -f "$CONFLICT_AUTO_APPROVAL_SCRIPT" ] && [ -f "$CONFLICT_RESOLUTION_APPROVAL_TEMPLATE" ] && [ -f "$CONFLICT_RESOLUTION_SCRIPT" ]; then
      $PY "$CONFLICT_AUTO_APPROVAL_SCRIPT" \
        --approval-template "$CONFLICT_RESOLUTION_APPROVAL_TEMPLATE" \
        --approval-json "$CONFLICT_AUTO_APPROVAL_JSON" \
        --report-json "$CONFLICT_AUTO_APPROVAL_REPORT" \
        --markdown "$CONFLICT_AUTO_APPROVAL_MD" \
        2>&1 | tee -a "$CF_SYNC_LOG"
      auto_approval_count="$($PY - "$CONFLICT_AUTO_APPROVAL_REPORT" <<'PY'
import json
import sys
from pathlib import Path
path = Path(sys.argv[1])
if not path.exists():
    print(0)
else:
    print(int((json.loads(path.read_text(encoding="utf-8"))).get("auto_approved_count") or 0))
PY
)"
      if [ "$auto_approval_count" != "0" ]; then
        $PY "$CONFLICT_RESOLUTION_SCRIPT" \
          --packet "$CONFLICT_REVIEW_JSON" \
          --month "$CF_MONTH" \
          --verified-voids-json "$VERIFIED_DUPLICATE_PM_VOIDS_JSON" \
          --source-transaction-index "$SOURCE_TRANSACTION_INDEX" \
          --approval-json "$CONFLICT_AUTO_APPROVAL_JSON" \
          --report-json "$CONFLICT_AUTO_APPLY_REPORT" \
          --report-markdown "$CONFLICT_AUTO_APPLY_MD" \
          --apply \
          2>&1 | tee -a "$CF_SYNC_LOG"
        set +e
        timeout --kill-after=30s "${CF_SYNC_TIMEOUT_SECONDS}s" $PY "$CF_SYNC_SCRIPT" \
          --gl-csv "$BASELANE_CF_REPORTING_LEDGER_PATH" \
          --month "$CF_MONTH" \
          "${CF_SYNC_MODE_ARGS[@]}" \
          --source-cash-gl-csv "$BASELANE_CF_REPORTING_LEDGER_PATH" \
          --skip-source-cash-balance-row \
          --output-dir "$DROPBOX_ROOT/Real Estate" \
          --conflict-threshold 0.01 \
          --alert-file "$ALERT_FILE" \
          2>&1 | tee -a "$CF_SYNC_LOG"
        cf_resync_rc="${PIPESTATUS[0]}"
        set -e
        if [ "$cf_resync_rc" -eq 0 ]; then
          latest_audit="$($PY - "$ROOT" <<'PY'
import sys
from pathlib import Path
root = Path(sys.argv[1])
files = sorted((root / "reports" / "cf_statement_sync").glob("audit_*.json"), key=lambda path: path.stat().st_mtime if path.exists() else 0)
print(files[-1] if files else "")
PY
)"
          if [ -n "$latest_audit" ] && [ -f "$CONFLICT_REVIEW_SCRIPT" ]; then
            $PY "$CONFLICT_REVIEW_SCRIPT" \
              --audit "$latest_audit" \
              --json "$CONFLICT_REVIEW_JSON" \
              --csv "$CONFLICT_REVIEW_CSV" \
              --markdown "$CONFLICT_REVIEW_MD" \
              2>&1 | tee -a "$CF_SYNC_LOG"
          fi
          $PY "$CONFLICT_RESOLUTION_SCRIPT" \
            --packet "$CONFLICT_REVIEW_JSON" \
            --month "$CF_MONTH" \
            --verified-voids-json "$VERIFIED_DUPLICATE_PM_VOIDS_JSON" \
            --source-transaction-index "$SOURCE_TRANSACTION_INDEX" \
            --approval-template-out "$CONFLICT_RESOLUTION_APPROVAL_TEMPLATE" \
            --report-json "$CONFLICT_RESOLUTION_REPORT" \
            --report-markdown "$CONFLICT_RESOLUTION_MD" \
            2>&1 | tee -a "$CF_SYNC_LOG"
        else
          if [ "$cf_resync_rc" -eq 124 ] || [ "$cf_resync_rc" -eq 137 ]; then
            write_cf_sync_report "failed" "$cf_resync_rc" "helper_timeout_after_auto_apply"
          else
            write_cf_sync_report "failed" "$cf_resync_rc" "helper_returned_nonzero_after_auto_apply"
          fi
          echo "[$(date -Is)] CF statement re-sync failed after auto-apply rc=$cf_resync_rc" >&2
          exit "$cf_resync_rc"
        fi
      fi
    fi
    write_cf_sync_report "ok" 0 ""
    echo "[$(date -Is)] CF statement sync complete" >&2
  else
    if [ "$cf_sync_rc" -eq 124 ] || [ "$cf_sync_rc" -eq 137 ]; then
      write_cf_sync_report "failed" "$cf_sync_rc" "helper_timeout"
    else
      write_cf_sync_report "failed" "$cf_sync_rc" "helper_returned_nonzero"
    fi
    echo "[$(date -Is)] CF statement sync failed rc=$cf_sync_rc" >&2
    exit "$cf_sync_rc"
  fi
else
  write_cf_sync_report "review_missing_script" 0 "missing_update_cf_statements"
  echo "[$(date -Is)] Skipping CF statement sync: $CF_SYNC_SCRIPT not found" >&2
fi

echo "[$(date -Is)] baselane_weekly_unprocessed_pass complete (candidate_count=$CAND_COUNT, duplicate_key_count=$DUP_COUNT)" >&2
