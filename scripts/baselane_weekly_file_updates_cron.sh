#!/usr/bin/env bash
set -euo pipefail

export PATH="${HOME}/.local/bin:/home/digit/.npm-global/bin:${HOME}/.npm-global/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${WORKSPACE_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
OPENCLAW_ROOT="${OPENCLAW_ROOT:-$(cd "$ROOT/.." && pwd)}"
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

PY="${PYTHON_BIN:-python3}"
case "$PY" in
  "/usr/bin/env python3"|"env python3") PY="python3" ;;
esac
CF_MONTH="${CF_MONTH:-${BASELANE_CF_MONTH:-$($PY - <<'PY'
from datetime import date, timedelta
today = date.today()
first_of_month = today.replace(day=1)
prior_closed_month = first_of_month - timedelta(days=1)
print(f"{prior_closed_month.year:04d}-{prior_closed_month.month:02d}")
PY
)}}"
export CF_MONTH
# Weekly reporting must be non-mutating unless a run explicitly authorizes live
# actions. Individual write flags remain required when the global approval is on.
BASELANE_WEEKLY_LIVE_ACTIONS_APPROVED="${BASELANE_WEEKLY_LIVE_ACTIONS_APPROVED:-0}"
BASELANE_FUTURE_CF_VALUES_APPLY="${BASELANE_FUTURE_CF_VALUES_APPLY:-0}"
BASELANE_WEEKLY_ALLOW_INCOMPLETE_MONTH="${BASELANE_WEEKLY_ALLOW_INCOMPLETE_MONTH:-0}"
if [ "$BASELANE_WEEKLY_LIVE_ACTIONS_APPROVED" != "1" ]; then
  export YHOME_GSHEET_APPLY=0
  export CF_BALANCE_SHEET_CASH_APPLY=0
  export CF_BALANCE_SHEET_CREATE_MISSING_ROWS=0
  export BASELANE_NATIVE_SPLIT_APPLY=0
  BASELANE_FUTURE_CF_VALUES_APPLY=0
fi
export BASELANE_WEEKLY_LIVE_ACTIONS_APPROVED BASELANE_FUTURE_CF_VALUES_APPLY
export BASELANE_WEEKLY_ALLOW_INCOMPLETE_MONTH
REPORT_DIR="$ROOT/reports"
STATE_FILE="$ROOT/scripts/.baselane_weekly_state"
RUN_REPORT_FILE="$REPORT_DIR/baselane_weekly_file_updates_run_report.json"
HISTORY_FILE="$REPORT_DIR/baselane_weekly_file_updates_run_history.jsonl"
SCOPE_GUARD_FILE="$REPORT_DIR/baselane_weekly_scope_guard.json"
DISK_PREFLIGHT_REPORT_FILE="$REPORT_DIR/baselane_weekly_disk_space_preflight_report.json"
BASELANE_DISK_PREFLIGHT_MIN_FREE_MIB="${BASELANE_DISK_PREFLIGHT_MIN_FREE_MIB:-10240}"
BASELANE_DISK_PREFLIGHT_PATH_TIMEOUT_SECONDS="${BASELANE_DISK_PREFLIGHT_PATH_TIMEOUT_SECONDS:-15}"
BASELANE_DISK_PREFLIGHT_TIMEOUT_SECONDS="${BASELANE_DISK_PREFLIGHT_TIMEOUT_SECONDS:-90}"
BASELANE_DISK_PREFLIGHT_KILL_AFTER_SECONDS="${BASELANE_DISK_PREFLIGHT_KILL_AFTER_SECONDS:-15}"
export BASELANE_DISK_PREFLIGHT_TIMEOUT_SECONDS BASELANE_DISK_PREFLIGHT_KILL_AFTER_SECONDS
STALE_FINANCIAL_ARTIFACT_GUARD_FILE="${STALE_FINANCIAL_ARTIFACT_GUARD_FILE:-$REPORT_DIR/baselane_stale_financial_artifact_guard.json}"
STALE_FINANCIAL_ARTIFACT_GUARD_TIMEOUT_SECONDS="${BASELANE_STALE_FINANCIAL_ARTIFACT_GUARD_TIMEOUT_SECONDS:-300}"
HOOK="$ROOT/scripts/baselane_weekly_unprocessed_pass.sh"
MORTGAGE_WORKFLOW_SCRIPT="${BASELANE_MORTGAGE_WORKFLOW_SCRIPT:-$ROOT/scripts/baselane_monthly_mortgage_workflow_idempotent.sh}"
MORTGAGE_WORKFLOW_REPORT="$REPORT_DIR/baselane_monthly_mortgage_workflow_gate_report.json"
BASELANE_WEEKLY_MORTGAGE_WORKFLOW_APPLY_TOKENOMICS="${BASELANE_WEEKLY_MORTGAGE_WORKFLOW_APPLY_TOKENOMICS:-1}"
ISO_WEEK="$(date +%G-%V)"
DOW="$(date +%u)"
FORCE_WEEKLY="${FORCE_WEEKLY_PASS:-0}"
ECOGL_SOURCE_FIX_EVIDENCE_TIMEOUT_SECONDS="${ECOGL_SOURCE_FIX_EVIDENCE_TIMEOUT_SECONDS:-180}"
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
STARTED_EPOCH="$(date +%s)"
STATUS="not_started"
RETURN_CODE=0
REASON=""
LAST_WEEK=""
DETERMINISTIC_VERIFICATION_IDEMPOTENT=false
DETERMINISTIC_VERIFICATION_IDEMPOTENT_REASON="not_run"
STATE_FILE_MARKED_COMPLETE=false
STATE_FILE_UNMARKED=false
STATE_FILE_UNMARKED_REASON=""
SCOPE_GUARD_RC=0
SCOPE_GUARD_ERROR=false
MORTGAGE_WORKFLOW_RC=0
MORTGAGE_WORKFLOW_STATUS="skipped"
DISK_PREFLIGHT_STATUS="not_started"
DISK_PREFLIGHT_RC=0
STALE_FINANCIAL_ARTIFACT_GUARD_STATUS="not_started"

append_review_reason() {
  local next_reason="$1"
  if [ -n "$REASON" ]; then
    REASON="${REASON};${next_reason}"
  else
    REASON="$next_reason"
  fi
}

mark_mortgage_workflow_review() {
  local next_reason="$1"
  if [ "$STATUS" != "failed" ]; then
    STATUS="review"
  fi
  append_review_reason "$next_reason"
}

run_mortgage_workflow_report_refresh() {
  if [ -x "$MORTGAGE_WORKFLOW_SCRIPT" ]; then
    set +e
    BASELANE_MORTGAGE_WORKFLOW_STATE_FILE="$ROOT/scripts/.baselane_weekly_mortgage_workflow_state" \
      BASELANE_MONTHLY_TARGET_STAMP="$ISO_WEEK" \
      BASELANE_MORTGAGE_STATEMENT_TARGET_MONTH="$CF_MONTH" \
      MORTGAGE_STATEMENT_TARGET_MONTH="$CF_MONTH" \
      MORTGAGE_WORKFLOW_REPORT_ONLY=1 \
      MORTGAGE_WORKFLOW_REPORT_ONLY_REASON="weekly-report-only-refresh" \
      MORTGAGE_WORKFLOW_REPORT_ONLY_APPLY_TOKENOMICS="$BASELANE_WEEKLY_MORTGAGE_WORKFLOW_APPLY_TOKENOMICS" \
      "$MORTGAGE_WORKFLOW_SCRIPT"
    MORTGAGE_WORKFLOW_RC="$?"
    set -e
    MORTGAGE_WORKFLOW_STATUS="ran"
    mortgage_report_hold_reason="$(mortgage_workflow_report_hold_reason)"
    if [ -n "$mortgage_report_hold_reason" ]; then
      mark_mortgage_workflow_review "$mortgage_report_hold_reason"
    fi
    if [ "$MORTGAGE_WORKFLOW_RC" -ne 0 ]; then
      mark_mortgage_workflow_review "mortgage_workflow_review"
    fi
  else
    MORTGAGE_WORKFLOW_STATUS="missing"
    if [ "$STATUS" != "failed" ]; then
      mark_mortgage_workflow_review "mortgage_workflow_missing"
    fi
  fi
}

mortgage_workflow_report_hold_reason() {
  "$PY" - "$MORTGAGE_WORKFLOW_REPORT" <<'PY'
import json
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])

def slug(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text or "unknown"

def intish(value: object) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0

if not path.exists():
    print("mortgage_workflow_report_missing")
    raise SystemExit(0)
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    print("mortgage_workflow_report_unreadable")
    raise SystemExit(0)

reasons = []
gate_status = data.get("status")
report_only_bookkeeping_review = (
    gate_status == "review"
    and data.get("report_only") is True
    and data.get("reason") in {"weekly-report-only-refresh", "report-only-refresh"}
    and data.get("tokenomics_workbook_write_guard_status") != "blocked"
    and data.get("coownership_tokenomics_workbook_write_ready") is not False
    and intish(data.get("coownership_tokenomics_workbook_write_blocker_count")) == 0
)
if gate_status and gate_status != "ok" and not report_only_bookkeeping_review:
    reasons.append(f"mortgage_workflow_gate_{slug(gate_status)}")
guard_status = data.get("tokenomics_workbook_write_guard_status")
if guard_status == "blocked":
    reasons.append("mortgage_workflow_tokenomics_workbook_write_guard_blocked")
coownership_gate_status = data.get("coownership_tokenomics_workbook_write_gate_status")
if coownership_gate_status and coownership_gate_status not in {"ok", "ready"}:
    reasons.append(f"coownership_tokenomics_workbook_write_{slug(coownership_gate_status)}")
if data.get("coownership_tokenomics_workbook_write_ready") is False:
    reasons.append("coownership_tokenomics_workbook_write_not_ready")

deduped = []
for reason in reasons:
    if reason not in deduped:
        deduped.append(reason)
print(";".join(deduped))
PY
}

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
  SCOPE_GUARD_RC="$?"
  set -e
  if [ "$SCOPE_GUARD_RC" -ne 0 ]; then
    STATUS="failed_scope_guard"
    REASON="staging_path_not_allowed"
    SCOPE_GUARD_ERROR=true
  fi
fi

write_report() {
  local status="$1"
  local rc="$2"
  local reason="${3:-}"
  mkdir -p "$REPORT_DIR" "$(dirname "$STATE_FILE")"
  BASELANE_WEEKLY_STATUS="$status" \
  BASELANE_WEEKLY_RC="$rc" \
  BASELANE_WEEKLY_REASON="$reason" \
  BASELANE_WEEKLY_STARTED_AT="$STARTED_AT" \
  BASELANE_WEEKLY_STARTED_EPOCH="$STARTED_EPOCH" \
  BASELANE_WEEKLY_ENDED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  BASELANE_WEEKLY_ENDED_EPOCH="$(date +%s)" \
  BASELANE_WEEKLY_REPORT_FILE="$RUN_REPORT_FILE" \
  BASELANE_WEEKLY_HISTORY_FILE="$HISTORY_FILE" \
  BASELANE_WEEKLY_HOOK="$HOOK" \
  BASELANE_WEEKLY_STATE_FILE="$STATE_FILE" \
  BASELANE_WEEKLY_ISO_WEEK="$ISO_WEEK" \
  BASELANE_WEEKLY_DOW="$DOW" \
  BASELANE_WEEKLY_LAST_WEEK="$LAST_WEEK" \
  BASELANE_WEEKLY_FORCE="$FORCE_WEEKLY" \
  BASELANE_WEEKLY_DETERMINISTIC_VERIFICATION_IDEMPOTENT="$DETERMINISTIC_VERIFICATION_IDEMPOTENT" \
  BASELANE_WEEKLY_DETERMINISTIC_VERIFICATION_IDEMPOTENT_REASON="$DETERMINISTIC_VERIFICATION_IDEMPOTENT_REASON" \
  BASELANE_WEEKLY_STATE_FILE_MARKED_COMPLETE="$STATE_FILE_MARKED_COMPLETE" \
  BASELANE_WEEKLY_STATE_FILE_UNMARKED="$STATE_FILE_UNMARKED" \
  BASELANE_WEEKLY_STATE_FILE_UNMARKED_REASON="$STATE_FILE_UNMARKED_REASON" \
  BASELANE_WEEKLY_DISK_PREFLIGHT_STATUS="$DISK_PREFLIGHT_STATUS" \
  BASELANE_WEEKLY_DISK_PREFLIGHT_RC="$DISK_PREFLIGHT_RC" \
  BASELANE_WEEKLY_DISK_PREFLIGHT_REPORT_FILE="$DISK_PREFLIGHT_REPORT_FILE" \
  BASELANE_WEEKLY_DISK_PREFLIGHT_MIN_FREE_MIB="$BASELANE_DISK_PREFLIGHT_MIN_FREE_MIB" \
  BASELANE_WEEKLY_STALE_FINANCIAL_ARTIFACT_GUARD_STATUS="$STALE_FINANCIAL_ARTIFACT_GUARD_STATUS" \
  BASELANE_WEEKLY_STALE_FINANCIAL_ARTIFACT_GUARD_FILE="$STALE_FINANCIAL_ARTIFACT_GUARD_FILE" \
  BASELANE_LEDGER_PATH="$BASELANE_LEDGER_PATH" \
  DROPBOX_ROOT="$DROPBOX_ROOT" \
  ROOT="$ROOT" \
  OPENCLAW_ROOT="$OPENCLAW_ROOT" \
  BASELANE_MORTGAGE_WORKFLOW_SCRIPT="$MORTGAGE_WORKFLOW_SCRIPT" \
  BASELANE_MORTGAGE_WORKFLOW_REPORT="$MORTGAGE_WORKFLOW_REPORT" \
  BASELANE_MORTGAGE_WORKFLOW_RC="$MORTGAGE_WORKFLOW_RC" \
  BASELANE_MORTGAGE_WORKFLOW_STATUS="$MORTGAGE_WORKFLOW_STATUS" \
  $PY - <<'PY'
import json
import os
from pathlib import Path

report_file = Path(os.environ["BASELANE_WEEKLY_REPORT_FILE"])
history_file = Path(os.environ["BASELANE_WEEKLY_HISTORY_FILE"])
root = Path(os.environ["ROOT"])
started_epoch = int(os.environ["BASELANE_WEEKLY_STARTED_EPOCH"])
ended_epoch = int(os.environ["BASELANE_WEEKLY_ENDED_EPOCH"])
weekly_report = root / "reports" / "baselane_weekly_unprocessed_report.json"
weekly_candidates = root / "reports" / "baselane_weekly_unprocessed_candidates.csv"
weekly_duplicate_review = root / "reports" / "baselane_weekly_duplicate_key_review.csv"
weekly_duplicate_allowlist_template = root / "reports" / "baselane_weekly_duplicate_allowlist_template.json"
weekly_duplicate_review_packet = root / "reports" / "baselane_weekly_duplicate_review_packet.md"
weekly_duplicate_allowlist = root / "scripts" / ".baselane_weekly_duplicate_allowlist.json"
weekly_raw_duplicate_report = root / "reports" / "baselane_weekly_raw_duplicate_report.json"
weekly_deduped_reporting_ledger = root / "reports" / "baselane_weekly_deduped_reporting_ledger.csv"
weekly_safe_category_reporting_ledger = root / "reports" / "baselane_weekly_safe_category_reporting_ledger.csv"
weekly_clean_reporting_ledger = root / "reports" / "baselane_weekly_clean_reporting_ledger.csv"
ecogl_safe_apply_report = root / "reports" / "baselane_ecogl_safe_category_apply_report.json"
ecogl_safe_apply_actions = root / "reports" / "baselane_ecogl_safe_category_apply_actions.csv"
ecogl_safe_apply_markdown = root / "reports" / "baselane_ecogl_safe_category_apply_report.md"
first_day_pm_fee_quarantine_report = root / "reports" / "baselane_first_day_pm_fee_quarantine_report.json"
first_day_pm_fee_quarantine_rows = root / "reports" / "baselane_first_day_pm_fee_quarantine_rows.csv"
first_day_pm_fee_quarantine_markdown = root / "reports" / "baselane_first_day_pm_fee_quarantine_report.md"
no_dao_mortgage_quarantine_report = root / "reports" / "baselane_no_dao_mortgage_reporting_quarantine.json"
no_dao_mortgage_quarantine_rows = root / "reports" / "baselane_no_dao_mortgage_reporting_quarantine_rows.csv"
no_dao_mortgage_quarantine_markdown = root / "reports" / "baselane_no_dao_mortgage_reporting_quarantine.md"
weekly_no_dao_mortgage_clean_reporting_ledger = root / "reports" / "baselane_weekly_no_dao_mortgage_clean_reporting_ledger.csv"
source_cleanup_queue_report = root / "reports" / "baselane_source_cleanup_queue.json"
source_cleanup_queue_rows = root / "reports" / "baselane_source_cleanup_queue.csv"
source_cleanup_queue_markdown = root / "reports" / "baselane_source_cleanup_queue.md"
ecogl_accrual_overlay_report = root / "reports" / "baselane_ecogl_accrual_overlay_report.json"
ecogl_accrual_overlay_actions = root / "reports" / "baselane_ecogl_accrual_overlay_actions.csv"
ecogl_accrual_overlay_markdown = root / "reports" / "baselane_ecogl_accrual_overlay_report.md"
weekly_accrual_overlay_ledger = root / "reports" / "baselane_weekly_accrual_overlay_reporting_ledger.csv"
cf_sync_report = root / "reports" / "baselane_weekly_cf_statement_sync_report.json"
cf_sync_review_packet = root / "reports" / "baselane_weekly_cf_statement_review_packet.md"
cf_no_gl_property_match_report = root / "reports" / "cf_statement_sync" / f"no_gl_property_match_{os.environ.get('CF_MONTH')}.json"
cf_no_gl_property_match_csv = cf_no_gl_property_match_report.with_suffix(".csv")
future_cf_values_apply_report = root / "reports" / "future_cf_statement_values_apply_report.json"
future_cf_values_report = root / "reports" / "future_cf_statement_values_clear_report.json"
cf_conflict_resolution_plan = root / "reports" / "baselane_cf_conflict_resolution_plan.json"
cf_conflict_resolution_markdown = root / "reports" / "baselane_cf_conflict_resolution_plan.md"
cf_conflict_resolution_approval_template = root / "reports" / "baselane_cf_conflict_resolution_approval_template.json"
ecogl_autonomy = root / "reports" / "baselane_ecogl_data_quality_autonomy.json"
ecogl_autonomy_markdown = root / "reports" / "baselane_ecogl_data_quality_autonomy.md"
ecogl_auto_safe_actions = root / "reports" / "baselane_ecogl_auto_safe_actions.csv"
ecogl_exceptions = root / "reports" / "baselane_ecogl_data_quality_exceptions.csv"
ecogl_source_fix_plan = root / "reports" / "baselane_ecogl_source_fix_plan.json"
ecogl_source_fix_actions = root / "reports" / "baselane_ecogl_source_fix_actions.csv"
ecogl_source_fix_markdown = root / "reports" / "baselane_ecogl_source_fix_plan.md"
ecogl_source_fix_evidence = root / "reports" / "baselane_ecogl_source_fix_evidence.json"
ecogl_source_fix_evidence_markdown = root / "reports" / "baselane_ecogl_source_fix_evidence.md"
ecogl_source_fix_verifier = root / "reports" / "baselane_ecogl_source_fix_verifier.json"
ecogl_source_fix_verifier_markdown = root / "reports" / "baselane_ecogl_source_fix_verifier.md"
ecogl_source_fix_corrections = root / "reports" / "baselane_ecogl_source_fix_corrections.json"
ecogl_source_fix_corrections_csv = root / "reports" / "baselane_ecogl_source_fix_corrections.csv"
ecogl_source_fix_corrections_markdown = root / "reports" / "baselane_ecogl_source_fix_corrections.md"
ecogl_source_fix_approval = root / "reports" / "baselane_ecogl_source_fix_approval.json"
ecogl_source_fix_approval_markdown = root / "reports" / "baselane_ecogl_source_fix_approval.md"
ecogl_source_fix_approved_corrections_csv = root / "reports" / "baselane_ecogl_source_fix_approved_corrections.csv"
ecogl_source_fix_correction_validation = root / "reports" / "baselane_ecogl_source_fix_correction_validation.json"
ecogl_source_fix_correction_validation_csv = root / "reports" / "baselane_ecogl_source_fix_correction_validation.csv"
ecogl_source_fix_correction_validation_markdown = root / "reports" / "baselane_ecogl_source_fix_correction_validation.md"
ecogl_source_fix_apply_plan = root / "reports" / "baselane_ecogl_source_fix_apply_plan.json"
ecogl_source_fix_apply_plan_csv = root / "reports" / "baselane_ecogl_source_fix_apply_plan.csv"
ecogl_source_fix_apply_plan_markdown = root / "reports" / "baselane_ecogl_source_fix_apply_plan.md"
ecogl_source_fix_apply = root / "reports" / "baselane_ecogl_source_fix_apply.json"
ecogl_source_fix_apply_csv = root / "reports" / "baselane_ecogl_source_fix_apply.csv"
ecogl_source_fix_apply_markdown = root / "reports" / "baselane_ecogl_source_fix_apply.md"
ecogl_source_fix_apply_payload = root / "reports" / "baselane_ecogl_source_fix_apply_payload.json"
ecogl_source_fix_action_queue = root / "reports" / "baselane_ecogl_source_fix_action_queue.json"
ecogl_source_fix_action_queue_csv = root / "reports" / "baselane_ecogl_source_fix_action_queue.csv"
ecogl_source_fix_action_queue_markdown = root / "reports" / "baselane_ecogl_source_fix_action_queue.md"
native_split_plan = root / "reports" / "baselane_native_split_plan.json"
native_split_plan_csv = root / "reports" / "baselane_native_split_plan.csv"
native_split_plan_markdown = root / "reports" / "baselane_native_split_plan.md"
native_split_apply = root / "reports" / "baselane_native_split_apply_report.json"
native_split_apply_state = root / "scripts" / ".baselane_native_split_apply_state.json"
cf_review_gate = root / "reports" / "baselane_weekly_cf_review_gate.json"
cf_review_gate_markdown = root / "reports" / "baselane_weekly_cf_review_gate.md"
cf_sync_log = root / "reports" / "cf_statement_sync.log"
mortgage_workflow_report = Path(os.environ["BASELANE_MORTGAGE_WORKFLOW_REPORT"])
mortgage_workflow_review_packet = root / "reports" / "baselane_monthly_mortgage_workflow_review_packet.md"
alert_file = root / "reports" / "baselane_weekly_alerts.txt"
report = {
    "job": "baselane-weekly-file-updates",
    "status": os.environ["BASELANE_WEEKLY_STATUS"],
    "return_code": int(os.environ["BASELANE_WEEKLY_RC"]),
    "reason": os.environ.get("BASELANE_WEEKLY_REASON") or None,
    "started_at": os.environ["BASELANE_WEEKLY_STARTED_AT"],
    "ended_at": os.environ["BASELANE_WEEKLY_ENDED_AT"],
    "duration_seconds": max(0, ended_epoch - started_epoch),
    "workspace_root": os.environ.get("ROOT"),
    "openclaw_root": os.environ.get("OPENCLAW_ROOT"),
    "dropbox_root": os.environ.get("DROPBOX_ROOT"),
    "baselane_ledger_path": os.environ.get("BASELANE_LEDGER_PATH"),
    "disk_space_preflight_status": os.environ.get("BASELANE_WEEKLY_DISK_PREFLIGHT_STATUS"),
    "disk_space_preflight_rc": int(os.environ.get("BASELANE_WEEKLY_DISK_PREFLIGHT_RC") or 0),
    "disk_space_preflight_report": os.environ.get("BASELANE_WEEKLY_DISK_PREFLIGHT_REPORT_FILE"),
    "disk_space_preflight_min_free_mib": int(os.environ.get("BASELANE_WEEKLY_DISK_PREFLIGHT_MIN_FREE_MIB") or 0),
    "stale_financial_artifact_guard_status": os.environ.get("BASELANE_WEEKLY_STALE_FINANCIAL_ARTIFACT_GUARD_STATUS"),
    "stale_financial_artifact_guard_file": os.environ.get("BASELANE_WEEKLY_STALE_FINANCIAL_ARTIFACT_GUARD_FILE"),
    "iso_week": os.environ["BASELANE_WEEKLY_ISO_WEEK"],
    "day_of_week": int(os.environ["BASELANE_WEEKLY_DOW"]),
    "last_completed_week": os.environ.get("BASELANE_WEEKLY_LAST_WEEK") or None,
    "force_weekly_pass": os.environ.get("BASELANE_WEEKLY_FORCE") == "1",
    "deterministic_verification_idempotent": os.environ.get("BASELANE_WEEKLY_DETERMINISTIC_VERIFICATION_IDEMPOTENT") == "true",
    "deterministic_verification_idempotent_reason": os.environ.get("BASELANE_WEEKLY_DETERMINISTIC_VERIFICATION_IDEMPOTENT_REASON") or None,
    "state_file_marked_complete": os.environ.get("BASELANE_WEEKLY_STATE_FILE_MARKED_COMPLETE") == "true",
    "state_file_unmarked": os.environ.get("BASELANE_WEEKLY_STATE_FILE_UNMARKED") == "true",
    "state_file_unmarked_reason": os.environ.get("BASELANE_WEEKLY_STATE_FILE_UNMARKED_REASON") or None,
    "hook": os.environ["BASELANE_WEEKLY_HOOK"],
    "state_file": os.environ["BASELANE_WEEKLY_STATE_FILE"],
    "mortgage_workflow_script": os.environ.get("BASELANE_MORTGAGE_WORKFLOW_SCRIPT"),
    "mortgage_workflow_status": os.environ.get("BASELANE_MORTGAGE_WORKFLOW_STATUS"),
    "mortgage_workflow_rc": int(os.environ.get("BASELANE_MORTGAGE_WORKFLOW_RC") or 0),
    "mortgage_workflow_report": str(mortgage_workflow_report),
    "artifacts": {
        "latest_report": str(report_file),
        "history": str(history_file),
        "disk_space_preflight": os.environ.get("BASELANE_WEEKLY_DISK_PREFLIGHT_REPORT_FILE"),
        "weekly_unprocessed_report": str(weekly_report),
        "weekly_unprocessed_candidates": str(weekly_candidates),
        "weekly_duplicate_key_review": str(weekly_duplicate_review),
        "weekly_duplicate_allowlist_template": str(weekly_duplicate_allowlist_template),
        "weekly_duplicate_review_packet": str(weekly_duplicate_review_packet),
        "weekly_duplicate_allowlist": str(weekly_duplicate_allowlist),
        "weekly_raw_duplicate_report": str(weekly_raw_duplicate_report),
        "weekly_deduped_reporting_ledger": str(weekly_deduped_reporting_ledger),
        "weekly_safe_category_reporting_ledger": str(weekly_safe_category_reporting_ledger),
        "weekly_clean_reporting_ledger": str(weekly_clean_reporting_ledger),
        "ecogl_safe_apply_report": str(ecogl_safe_apply_report),
        "ecogl_safe_apply_actions": str(ecogl_safe_apply_actions),
        "ecogl_safe_apply_markdown": str(ecogl_safe_apply_markdown),
        "first_day_pm_fee_quarantine_report": str(first_day_pm_fee_quarantine_report),
        "first_day_pm_fee_quarantine_rows": str(first_day_pm_fee_quarantine_rows),
        "first_day_pm_fee_quarantine_markdown": str(first_day_pm_fee_quarantine_markdown),
        "no_dao_mortgage_quarantine_report": str(no_dao_mortgage_quarantine_report),
        "no_dao_mortgage_quarantine_rows": str(no_dao_mortgage_quarantine_rows),
        "no_dao_mortgage_quarantine_markdown": str(no_dao_mortgage_quarantine_markdown),
        "weekly_no_dao_mortgage_clean_reporting_ledger": str(weekly_no_dao_mortgage_clean_reporting_ledger),
        "ecogl_accrual_overlay_report": str(ecogl_accrual_overlay_report),
        "ecogl_accrual_overlay_actions": str(ecogl_accrual_overlay_actions),
        "ecogl_accrual_overlay_markdown": str(ecogl_accrual_overlay_markdown),
        "weekly_accrual_overlay_ledger": str(weekly_accrual_overlay_ledger),
        "cf_statement_sync_report": str(cf_sync_report),
        "cf_statement_sync_review_packet": str(cf_sync_review_packet),
        "cf_conflict_resolution_plan": str(cf_conflict_resolution_plan),
        "cf_conflict_resolution_markdown": str(cf_conflict_resolution_markdown),
        "cf_conflict_resolution_approval_template": str(cf_conflict_resolution_approval_template),
        "ecogl_autonomy": str(ecogl_autonomy),
        "ecogl_autonomy_markdown": str(ecogl_autonomy_markdown),
        "ecogl_auto_safe_actions": str(ecogl_auto_safe_actions),
        "ecogl_exceptions": str(ecogl_exceptions),
        "ecogl_source_fix_plan": str(ecogl_source_fix_plan),
        "ecogl_source_fix_actions": str(ecogl_source_fix_actions),
        "ecogl_source_fix_markdown": str(ecogl_source_fix_markdown),
        "ecogl_source_fix_evidence": str(ecogl_source_fix_evidence),
        "ecogl_source_fix_evidence_markdown": str(ecogl_source_fix_evidence_markdown),
        "ecogl_source_fix_verifier": str(ecogl_source_fix_verifier),
        "ecogl_source_fix_verifier_markdown": str(ecogl_source_fix_verifier_markdown),
        "ecogl_source_fix_corrections": str(ecogl_source_fix_corrections),
        "ecogl_source_fix_corrections_csv": str(ecogl_source_fix_corrections_csv),
        "ecogl_source_fix_corrections_markdown": str(ecogl_source_fix_corrections_markdown),
        "ecogl_source_fix_approval": str(ecogl_source_fix_approval),
        "ecogl_source_fix_approval_markdown": str(ecogl_source_fix_approval_markdown),
        "ecogl_source_fix_approved_corrections_csv": str(ecogl_source_fix_approved_corrections_csv),
        "ecogl_source_fix_correction_validation": str(ecogl_source_fix_correction_validation),
        "ecogl_source_fix_correction_validation_csv": str(ecogl_source_fix_correction_validation_csv),
        "ecogl_source_fix_correction_validation_markdown": str(ecogl_source_fix_correction_validation_markdown),
        "ecogl_source_fix_apply_plan": str(ecogl_source_fix_apply_plan),
        "ecogl_source_fix_apply_plan_csv": str(ecogl_source_fix_apply_plan_csv),
        "ecogl_source_fix_apply_plan_markdown": str(ecogl_source_fix_apply_plan_markdown),
        "ecogl_source_fix_apply": str(ecogl_source_fix_apply),
        "ecogl_source_fix_apply_csv": str(ecogl_source_fix_apply_csv),
        "ecogl_source_fix_apply_markdown": str(ecogl_source_fix_apply_markdown),
        "ecogl_source_fix_apply_payload": str(ecogl_source_fix_apply_payload),
        "ecogl_source_fix_action_queue": str(ecogl_source_fix_action_queue),
        "ecogl_source_fix_action_queue_csv": str(ecogl_source_fix_action_queue_csv),
        "ecogl_source_fix_action_queue_markdown": str(ecogl_source_fix_action_queue_markdown),
        "native_split_plan": str(native_split_plan),
        "native_split_plan_csv": str(native_split_plan_csv),
        "native_split_plan_markdown": str(native_split_plan_markdown),
        "native_split_apply": str(native_split_apply),
        "native_split_apply_state": str(native_split_apply_state),
        "cf_review_gate": str(cf_review_gate),
        "cf_review_gate_markdown": str(cf_review_gate_markdown),
        "cf_statement_sync_log": str(cf_sync_log),
        "mortgage_workflow_report": str(mortgage_workflow_report),
        "mortgage_workflow_review_packet": str(mortgage_workflow_review_packet),
        "weekly_alerts": str(alert_file),
    },
}

def read_json(path_text: object) -> dict:
    try:
        data = json.loads(Path(str(path_text)).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}

disk_preflight = read_json(report.get("disk_space_preflight_report") or "")
disk_preflight_next_action = str(disk_preflight.get("next_action") or "").strip()
report["disk_space_preflight_next_action"] = disk_preflight_next_action or None
report["disk_space_preflight_required_free_mib"] = disk_preflight.get("required_free_mib")
report["mortgage_workflow_report_exists"] = mortgage_workflow_report.exists()
report["mortgage_workflow_report_readable"] = False
if mortgage_workflow_report.exists():
    try:
        mortgage_data = json.loads(mortgage_workflow_report.read_text(encoding="utf-8"))
        if not isinstance(mortgage_data, dict):
            raise ValueError("mortgage workflow report is not a JSON object")
        report["mortgage_workflow_report_readable"] = True
        def mortgage_field(primary, fallback=None):
            value = mortgage_data.get(primary)
            if value is None and fallback:
                return mortgage_data.get(fallback)
            return value
        def compact_rows(value):
            return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
        def item_property(item):
            return item.get("property") or item.get("property_name") or item.get("canonical_property")
        def unique_values(values):
            result = []
            for value in values:
                if isinstance(value, list):
                    nested = value
                else:
                    nested = [value]
                for item in nested:
                    if item in (None, "", []):
                        continue
                    if item not in result:
                        result.append(item)
            return result
        def unique_row_values(rows, key):
            return unique_values(item.get(key) for item in rows)
        def unique_row_properties(rows, key, expected=None):
            return unique_values(
                item_property(item)
                for item in rows
                if item.get(key) is not None and (expected is None or item.get(key) == expected)
            )
        def candidate_values(rows, key):
            values = []
            for item in rows:
                candidates = item.get("target_month_statement_candidates")
                if not isinstance(candidates, list):
                    continue
                for candidate in candidates:
                    if isinstance(candidate, dict):
                        values.append(candidate.get(key))
            return unique_values(values)
        report["mortgage_workflow_gate_status"] = mortgage_data.get("status")
        report["mortgage_workflow_gate_reason"] = mortgage_data.get("reason")
        report["mortgage_workflow_gate_stamp"] = mortgage_data.get("stamp")
        report["mortgage_workflow_gate_idempotency_digest"] = mortgage_data.get("idempotency_digest")
        report["mortgage_workflow_mortgage_statement_target_month"] = mortgage_data.get("mortgage_statement_target_month")
        report["mortgage_workflow_mortgage_statement_target_month_kind"] = mortgage_data.get("mortgage_statement_target_month_kind")
        report["mortgage_workflow_mortgage_statement_target_month_basis"] = mortgage_data.get("mortgage_statement_target_month_basis")
        report["mortgage_workflow_mortgage_workflow_month_semantics"] = mortgage_data.get("mortgage_workflow_month_semantics")
        report["mortgage_workflow_citadel_statement_target_month"] = mortgage_data.get("citadel_statement_target_month")
        report["mortgage_workflow_report_only"] = mortgage_data.get("report_only")
        report["mortgage_workflow_live_stages_skipped"] = mortgage_data.get("live_stages_skipped")
        report["mortgage_workflow_mortgage_downloader_execution_status"] = mortgage_data.get("mortgage_downloader_execution_status")
        report["mortgage_workflow_mortgage_downloader_report_is_live_for_this_run"] = mortgage_data.get("mortgage_downloader_report_is_live_for_this_run")
        report["mortgage_workflow_tokenomics_workbook_write_is_live_for_this_run"] = mortgage_data.get("tokenomics_workbook_write_is_live_for_this_run")
        report["mortgage_workflow_downloader_rc"] = mortgage_data.get("downloader_rc")
        report["mortgage_workflow_downloader_credential_refresh_report"] = mortgage_data.get("mortgage_downloader_credential_refresh_report")
        report["mortgage_workflow_downloader_credential_refresh_rc"] = mortgage_data.get("mortgage_downloader_credential_refresh_rc")
        report["mortgage_workflow_downloader_credential_refresh_status"] = mortgage_data.get("mortgage_downloader_credential_refresh_status")
        report["mortgage_workflow_downloader_credential_refresh_profile"] = mortgage_data.get("mortgage_downloader_credential_refresh_profile")
        report["mortgage_workflow_downloader_credential_refresh_ids"] = mortgage_data.get("mortgage_downloader_credential_refresh_ids")
        report["mortgage_workflow_downloader_credential_refresh_timeout_seconds"] = mortgage_data.get("mortgage_downloader_credential_refresh_timeout_seconds")
        report["mortgage_workflow_citadel_download_rc"] = mortgage_data.get("citadel_download_rc")
        report["mortgage_workflow_splits_rc"] = mortgage_data.get("mortgage_splits_rc")
        report["mortgage_workflow_splits_report"] = mortgage_data.get("mortgage_splits_report")
        report["mortgage_workflow_splits_status"] = mortgage_data.get("mortgage_splits_status")
        report["mortgage_workflow_splits_classification"] = mortgage_data.get("mortgage_splits_classification")
        report["mortgage_workflow_splits_issue_count"] = mortgage_data.get("mortgage_splits_issue_count")
        report["mortgage_workflow_splits_ok_count"] = mortgage_data.get("mortgage_splits_ok_count")
        report["mortgage_workflow_splits_safe_to_run_automatically"] = mortgage_data.get("mortgage_splits_safe_to_run_automatically")
        report["mortgage_workflow_splits_review_command"] = mortgage_data.get("mortgage_splits_review_command")
        report["mortgage_workflow_splits_review_command_valid"] = mortgage_data.get("mortgage_splits_review_command_valid")
        report["mortgage_workflow_tokenomics_rc"] = mortgage_data.get("tokenomics_rc")
        report["mortgage_workflow_downloader_coverage_rc"] = mortgage_data.get("mortgage_downloader_coverage_rc")
        report["mortgage_workflow_evidence_advance_rc"] = mortgage_data.get("mortgage_workflow_evidence_advance_rc")
        report["mortgage_workflow_evidence_refresh_rc"] = mortgage_data.get("mortgage_workflow_evidence_refresh_rc")
        report["mortgage_workflow_replayability_rc"] = mortgage_data.get("mortgage_workflow_replayability_rc")
        report["mortgage_workflow_tokenomics_report"] = mortgage_data.get("tokenomics_report")
        report["mortgage_workflow_tokenomics_workbook_write_guard_status"] = mortgage_data.get("tokenomics_workbook_write_guard_status")
        report["mortgage_workflow_tokenomics_workbook_write_guard_reason"] = mortgage_data.get("tokenomics_workbook_write_guard_reason")
        report["mortgage_workflow_tokenomics_infra_review_ignored_for_workbook_write"] = mortgage_data.get("tokenomics_infra_review_ignored_for_workbook_write")
        report["mortgage_workflow_tokenomics_workbook_write_ready"] = mortgage_data.get("tokenomics_workbook_write_ready")
        report["mortgage_workflow_tokenomics_workbook_write_gate_status"] = mortgage_data.get("tokenomics_workbook_write_gate_status")
        report["mortgage_workflow_tokenomics_workbook_write_ready_property_count"] = mortgage_data.get("tokenomics_workbook_write_ready_property_count")
        report["mortgage_workflow_tokenomics_workbook_write_ready_properties"] = mortgage_data.get("tokenomics_workbook_write_ready_properties")
        report["mortgage_workflow_tokenomics_workbook_ready_property_write_plan_count"] = mortgage_data.get("tokenomics_workbook_ready_property_write_plan_count")
        report["mortgage_workflow_tokenomics_workbook_ready_property_write_plan_properties"] = mortgage_data.get("tokenomics_workbook_ready_property_write_plan_properties")
        report["mortgage_workflow_tokenomics_workbook_ready_property_write_plan"] = mortgage_data.get("tokenomics_workbook_ready_property_write_plan")
        report["mortgage_workflow_tokenomics_workbook_ready_property_planned_cell_write_count"] = mortgage_data.get("tokenomics_workbook_ready_property_planned_cell_write_count")
        report["mortgage_workflow_tokenomics_workbook_ready_property_write_blocked_by_full_gate"] = mortgage_data.get("tokenomics_workbook_ready_property_write_blocked_by_full_gate")
        report["mortgage_workflow_tokenomics_workbook_write_blocker_count"] = mortgage_data.get("tokenomics_workbook_write_blocker_count")
        report["mortgage_workflow_tokenomics_workbook_write_blocker_properties"] = mortgage_data.get("tokenomics_workbook_write_blocker_properties")
        report["mortgage_workflow_tokenomics_workbook_write_blocker_reasons"] = mortgage_data.get("tokenomics_workbook_write_blocker_reasons")
        report["mortgage_workflow_tokenomics_workbook_write_blockers"] = mortgage_data.get("tokenomics_workbook_write_blockers")
        report["mortgage_workflow_downloader_tokenomics_blocking_gap_count"] = mortgage_data.get("mortgage_downloader_tokenomics_blocking_gap_count")
        report["mortgage_workflow_downloader_tokenomics_blocking_gap_properties"] = mortgage_data.get("mortgage_downloader_tokenomics_blocking_gap_properties")
        report["mortgage_workflow_downloader_citadel_tokenomics_blocking"] = mortgage_data.get("mortgage_downloader_citadel_tokenomics_blocking")
        report["mortgage_workflow_downloader_coverage_report"] = mortgage_data.get("mortgage_downloader_coverage_report")
        report["mortgage_workflow_evidence_advance_report"] = mortgage_data.get("mortgage_workflow_evidence_advance_report")
        report["mortgage_workflow_evidence_refresh_report"] = mortgage_data.get("mortgage_workflow_evidence_refresh_report")
        report["mortgage_workflow_evidence_refresh_idempotency_digest"] = mortgage_data.get("mortgage_workflow_evidence_refresh_idempotency_digest")
        report["mortgage_workflow_replayability_report"] = mortgage_data.get("mortgage_workflow_replayability_report")
        report["mortgage_workflow_tokenomics_status"] = mortgage_data.get("tokenomics_status")
        report["mortgage_workflow_tokenomics_effective_status"] = mortgage_data.get("tokenomics_effective_status")
        report["mortgage_workflow_tokenomics_current_month"] = mortgage_data.get("tokenomics_current_month")
        report["mortgage_workflow_tokenomics_workbook_path"] = mortgage_data.get("tokenomics_workbook_path")
        report["mortgage_workflow_tokenomics_workbook_save_attempted"] = mortgage_data.get("tokenomics_workbook_save_attempted")
        report["mortgage_workflow_tokenomics_workbook_partial_ready_property_write_attempted"] = mortgage_data.get("tokenomics_workbook_partial_ready_property_write_attempted")
        report["mortgage_workflow_tokenomics_workbook_partial_ready_property_write_applied"] = mortgage_data.get("tokenomics_workbook_partial_ready_property_write_applied")
        report["mortgage_workflow_tokenomics_workbook_partial_ready_property_write_reason"] = mortgage_data.get("tokenomics_workbook_partial_ready_property_write_reason")
        report["mortgage_workflow_tokenomics_workbook_partial_ready_property_write_property_count"] = mortgage_data.get("tokenomics_workbook_partial_ready_property_write_property_count")
        report["mortgage_workflow_tokenomics_workbook_partial_ready_property_write_properties"] = mortgage_data.get("tokenomics_workbook_partial_ready_property_write_properties")
        report["mortgage_workflow_tokenomics_workbook_partial_ready_property_cleared_copied_stale_property_count"] = mortgage_data.get("tokenomics_workbook_partial_ready_property_cleared_copied_stale_property_count")
        report["mortgage_workflow_tokenomics_workbook_partial_ready_property_cleared_copied_stale_properties"] = mortgage_data.get("tokenomics_workbook_partial_ready_property_cleared_copied_stale_properties")
        report["mortgage_workflow_tokenomics_created_sheet"] = mortgage_data.get("tokenomics_created_sheet")
        report["mortgage_workflow_tokenomics_property_column_count"] = mortgage_data.get("tokenomics_property_column_count")
        report["mortgage_workflow_tokenomics_co_owner_paid_property_count"] = mortgage_data.get("tokenomics_co_owner_paid_property_count")
        report["mortgage_workflow_tokenomics_skipped_non_co_owner_paid_property_count"] = mortgage_data.get("tokenomics_skipped_non_co_owner_paid_property_count")
        report["mortgage_workflow_tokenomics_co_owner_paid_property_alignment_status"] = mortgage_data.get("tokenomics_co_owner_paid_property_alignment_status")
        report["mortgage_workflow_tokenomics_co_owner_paid_property_alignment_mismatch_count"] = mortgage_data.get("tokenomics_co_owner_paid_property_alignment_mismatch_count")
        report["mortgage_workflow_tokenomics_co_owner_paid_property_alignment_missing_from_coverage"] = mortgage_data.get("tokenomics_co_owner_paid_property_alignment_missing_from_coverage")
        report["mortgage_workflow_tokenomics_co_owner_paid_property_alignment_missing_from_tokenomics"] = mortgage_data.get("tokenomics_co_owner_paid_property_alignment_missing_from_tokenomics")
        report["mortgage_workflow_tokenomics_co_owner_paid_property_alignment_tokenomics_properties"] = mortgage_data.get("tokenomics_co_owner_paid_property_alignment_tokenomics_properties")
        report["mortgage_workflow_tokenomics_co_owner_paid_property_alignment_coverage_properties"] = mortgage_data.get("tokenomics_co_owner_paid_property_alignment_coverage_properties")
        report["mortgage_workflow_tokenomics_co_owner_paid_property_alignment_coverage_workbook_path"] = mortgage_data.get("tokenomics_co_owner_paid_property_alignment_coverage_workbook_path")
        report["mortgage_workflow_tokenomics_statement_found_count"] = mortgage_data.get("tokenomics_statement_found_count")
        report["mortgage_workflow_tokenomics_current_month_statement_found_count"] = mortgage_data.get("tokenomics_current_month_statement_found_count")
        report["mortgage_workflow_tokenomics_current_month_statement_gap_count"] = mortgage_data.get("tokenomics_current_month_statement_gap_count")
        report["mortgage_workflow_tokenomics_current_month_statement_gap_properties"] = mortgage_data.get("tokenomics_current_month_statement_gap_properties")
        report["mortgage_workflow_tokenomics_current_month_statement_gaps"] = mortgage_data.get("tokenomics_current_month_statement_gaps")
        report["mortgage_workflow_tokenomics_current_month_statement_gap_remediation_count"] = mortgage_data.get("tokenomics_current_month_statement_gap_remediation_count")
        report["mortgage_workflow_tokenomics_current_month_statement_gap_remediation_available_count"] = mortgage_data.get("tokenomics_current_month_statement_gap_remediation_available_count")
        report["mortgage_workflow_tokenomics_current_month_statement_gap_remediation_missing_action_count"] = mortgage_data.get("tokenomics_current_month_statement_gap_remediation_missing_action_count")
        report["mortgage_workflow_tokenomics_current_month_statement_gap_remediation_missing_action_properties"] = mortgage_data.get("tokenomics_current_month_statement_gap_remediation_missing_action_properties")
        report["mortgage_workflow_tokenomics_current_month_statement_gap_remediation_plan"] = mortgage_data.get("tokenomics_current_month_statement_gap_remediation_plan")
        report["mortgage_workflow_tokenomics_mortgage_downloader_coverage_report"] = mortgage_data.get("tokenomics_mortgage_downloader_coverage_report")
        report["mortgage_workflow_tokenomics_mortgage_downloader_coverage_status"] = mortgage_data.get("tokenomics_mortgage_downloader_coverage_status")
        report["mortgage_workflow_tokenomics_mortgage_downloader_coverage_report_readable"] = mortgage_data.get("tokenomics_mortgage_downloader_coverage_report_readable")
        report["mortgage_workflow_tokenomics_stale_statement_count"] = mortgage_data.get("tokenomics_stale_statement_count")
        report["mortgage_workflow_tokenomics_stale_statement_properties"] = mortgage_data.get("tokenomics_stale_statement_properties")
        report["mortgage_workflow_tokenomics_missing_current_month_statement_count"] = mortgage_data.get("tokenomics_missing_current_month_statement_count")
        report["mortgage_workflow_tokenomics_missing_current_month_statement_properties"] = mortgage_data.get("tokenomics_missing_current_month_statement_properties")
        report["mortgage_workflow_tokenomics_pdf_statement_count"] = mortgage_data.get("tokenomics_pdf_statement_count")
        report["mortgage_workflow_tokenomics_statements_found"] = mortgage_data.get("tokenomics_statements_found")
        report["mortgage_workflow_tokenomics_balances_written"] = mortgage_data.get("tokenomics_balances_written")
        report["mortgage_workflow_tokenomics_statement_reference_write_count"] = mortgage_data.get("tokenomics_statement_reference_write_count")
        report["mortgage_workflow_tokenomics_principal_balance_write_count"] = mortgage_data.get("tokenomics_principal_balance_write_count")
        report["mortgage_workflow_tokenomics_cell_write_count"] = mortgage_data.get("tokenomics_cell_write_count")
        report["mortgage_workflow_tokenomics_safe_to_run_automatically"] = mortgage_data.get("tokenomics_safe_to_run_automatically")
        report["mortgage_workflow_tokenomics_statement_record_count"] = mortgage_data.get("tokenomics_statement_record_count")
        report["mortgage_workflow_tokenomics_co_owner_paid_statement_record_count"] = mortgage_data.get("tokenomics_co_owner_paid_statement_record_count")
        report["mortgage_workflow_tokenomics_co_owner_paid_statement_records"] = mortgage_data.get("tokenomics_co_owner_paid_statement_records")
        report["mortgage_workflow_tokenomics_missing_co_owner_paid_statement_properties"] = mortgage_data.get("tokenomics_missing_co_owner_paid_statement_properties")
        report["mortgage_workflow_downloader_coverage_status"] = mortgage_data.get("mortgage_downloader_coverage_status")
        report["mortgage_workflow_downloader_coverage_tokenomics_source"] = mortgage_data.get("mortgage_downloader_coverage_tokenomics_source")
        report["mortgage_workflow_downloader_coverage_tokenomics_fallback_report"] = mortgage_data.get("mortgage_downloader_coverage_tokenomics_fallback_report")
        report["mortgage_workflow_downloader_coverage_tokenomics_fallback_report_used"] = mortgage_data.get("mortgage_downloader_coverage_tokenomics_fallback_report_used")
        report["mortgage_workflow_downloader_coverage_tokenomics_fallback_error"] = mortgage_data.get("mortgage_downloader_coverage_tokenomics_fallback_error")
        report["mortgage_workflow_downloader_coverage_config_readable"] = mortgage_data.get("mortgage_downloader_coverage_config_readable")
        report["mortgage_workflow_downloader_coverage_config_error"] = mortgage_data.get("mortgage_downloader_coverage_config_error")
        report["mortgage_workflow_downloader_coverage_co_owner_paid_property_count"] = mortgage_data.get("mortgage_downloader_coverage_co_owner_paid_property_count")
        report["mortgage_workflow_downloader_coverage_covered_co_owner_paid_property_count"] = mortgage_data.get("mortgage_downloader_coverage_covered_co_owner_paid_property_count")
        report["mortgage_workflow_downloader_coverage_automation_ready_co_owner_paid_property_count"] = mortgage_data.get("mortgage_downloader_coverage_automation_ready_co_owner_paid_property_count")
        report["mortgage_workflow_downloader_coverage_configured_but_not_ready_co_owner_paid_property_count"] = mortgage_data.get("mortgage_downloader_coverage_configured_but_not_ready_co_owner_paid_property_count")
        report["mortgage_workflow_downloader_coverage_configured_downloader_count"] = mortgage_data.get("mortgage_downloader_coverage_configured_downloader_count")
        report["mortgage_workflow_downloader_coverage_configured_downloader_ids"] = mortgage_data.get("mortgage_downloader_coverage_configured_downloader_ids")
        report["mortgage_workflow_downloader_coverage_configured_downloader_properties"] = mortgage_data.get("mortgage_downloader_coverage_configured_downloader_properties")
        report["mortgage_workflow_downloader_coverage_configured_downloader_latest_report_statuses"] = mortgage_data.get("mortgage_downloader_coverage_configured_downloader_latest_report_statuses")
        report["mortgage_workflow_downloader_coverage_configured_downloader_latest_report_reasons"] = mortgage_data.get("mortgage_downloader_coverage_configured_downloader_latest_report_reasons")
        report["mortgage_workflow_downloader_coverage_configured_downloader_blocker_reasons"] = mortgage_data.get("mortgage_downloader_coverage_configured_downloader_blocker_reasons")
        report["mortgage_workflow_downloader_coverage_active_configured_downloader_count"] = mortgage_data.get("mortgage_downloader_coverage_active_configured_downloader_count")
        report["mortgage_workflow_downloader_coverage_active_configured_downloader_ids"] = mortgage_data.get("mortgage_downloader_coverage_active_configured_downloader_ids")
        report["mortgage_workflow_downloader_coverage_automation_ready_configured_downloader_count"] = mortgage_data.get("mortgage_downloader_coverage_automation_ready_configured_downloader_count")
        report["mortgage_workflow_downloader_coverage_automation_ready_configured_downloader_ids"] = mortgage_data.get("mortgage_downloader_coverage_automation_ready_configured_downloader_ids")
        report["mortgage_workflow_downloader_coverage_automation_blocked_configured_downloader_count"] = mortgage_data.get("mortgage_downloader_coverage_automation_blocked_configured_downloader_count")
        report["mortgage_workflow_downloader_coverage_automation_blocked_configured_downloader_ids"] = mortgage_data.get("mortgage_downloader_coverage_automation_blocked_configured_downloader_ids")
        report["mortgage_workflow_downloader_coverage_active_downloader_mode_counts"] = mortgage_data.get("mortgage_downloader_coverage_active_downloader_mode_counts")
        report["mortgage_workflow_downloader_coverage_automation_ready_downloader_mode_counts"] = mortgage_data.get("mortgage_downloader_coverage_automation_ready_downloader_mode_counts")
        report["mortgage_workflow_downloader_coverage_active_har_backed_offline_extractor_count"] = mortgage_data.get("mortgage_downloader_coverage_active_har_backed_offline_extractor_count")
        report["mortgage_workflow_downloader_coverage_automation_ready_har_backed_offline_extractor_count"] = mortgage_data.get("mortgage_downloader_coverage_automation_ready_har_backed_offline_extractor_count")
        report["mortgage_workflow_downloader_coverage_active_live_cdp_portal_downloader_count"] = mortgage_data.get("mortgage_downloader_coverage_active_live_cdp_portal_downloader_count")
        report["mortgage_workflow_downloader_coverage_automation_ready_live_cdp_portal_downloader_count"] = mortgage_data.get("mortgage_downloader_coverage_automation_ready_live_cdp_portal_downloader_count")
        report["mortgage_workflow_downloader_coverage_current_month_statement_available_count"] = mortgage_data.get("mortgage_downloader_coverage_current_month_statement_available_count")
        report["mortgage_workflow_downloader_coverage_current_month_statement_gap_count"] = mortgage_data.get("mortgage_downloader_coverage_current_month_statement_gap_count")
        report["mortgage_workflow_downloader_coverage_current_month_statement_gap_properties"] = mortgage_data.get("mortgage_downloader_coverage_current_month_statement_gap_properties")
        report["mortgage_workflow_downloader_coverage_current_month_statement_gaps"] = mortgage_data.get("mortgage_downloader_coverage_current_month_statement_gaps")
        report["mortgage_workflow_downloader_coverage_downloader_target_month_statement_gap_count"] = mortgage_data.get("mortgage_downloader_coverage_downloader_target_month_statement_gap_count")
        report["mortgage_workflow_downloader_coverage_downloader_target_month_statement_gap_properties"] = mortgage_data.get("mortgage_downloader_coverage_downloader_target_month_statement_gap_properties")
        report["mortgage_workflow_downloader_coverage_downloader_target_month_statement_gaps"] = mortgage_data.get("mortgage_downloader_coverage_downloader_target_month_statement_gaps")
        report["mortgage_workflow_downloader_coverage_stale_statement_count"] = mortgage_data.get("mortgage_downloader_coverage_stale_statement_count")
        report["mortgage_workflow_downloader_coverage_stale_statement_properties"] = mortgage_data.get("mortgage_downloader_coverage_stale_statement_properties")
        report["mortgage_workflow_downloader_coverage_missing_current_month_statement_count"] = mortgage_data.get("mortgage_downloader_coverage_missing_current_month_statement_count")
        report["mortgage_workflow_downloader_coverage_missing_current_month_statement_properties"] = mortgage_data.get("mortgage_downloader_coverage_missing_current_month_statement_properties")
        report["mortgage_workflow_downloader_coverage_blocked_configured_downloader_count"] = mortgage_data.get("mortgage_downloader_coverage_blocked_configured_downloader_count")
        report["mortgage_workflow_downloader_coverage_blocked_configured_downloaders"] = mortgage_data.get("mortgage_downloader_coverage_blocked_configured_downloaders")
        report["mortgage_workflow_downloader_coverage_automation_repair_not_statement_gap_count"] = mortgage_data.get("mortgage_downloader_coverage_automation_repair_not_statement_gap_count")
        report["mortgage_workflow_downloader_coverage_automation_repair_not_statement_gap_properties"] = mortgage_data.get("mortgage_downloader_coverage_automation_repair_not_statement_gap_properties")
        report["mortgage_workflow_downloader_coverage_automation_repair_not_statement_gap_downloaders"] = mortgage_data.get("mortgage_downloader_coverage_automation_repair_not_statement_gap_downloaders")
        report["mortgage_workflow_downloader_coverage_unexpected_configured_downloader_count"] = mortgage_data.get("mortgage_downloader_coverage_unexpected_configured_downloader_count")
        report["mortgage_workflow_downloader_coverage_unexpected_configured_downloader_properties"] = mortgage_data.get("mortgage_downloader_coverage_unexpected_configured_downloader_properties")
        report["mortgage_workflow_downloader_coverage_unexpected_configured_downloaders"] = mortgage_data.get("mortgage_downloader_coverage_unexpected_configured_downloaders")
        report["mortgage_workflow_downloader_coverage_missing_downloader_count"] = mortgage_data.get("mortgage_downloader_coverage_missing_downloader_count")
        report["mortgage_workflow_downloader_coverage_missing_downloader_properties"] = mortgage_data.get("mortgage_downloader_coverage_missing_downloader_properties")
        report["mortgage_workflow_downloader_coverage_workflow_evidence_capture_plan_count"] = mortgage_data.get("mortgage_downloader_coverage_workflow_evidence_capture_plan_count")
        report["mortgage_workflow_downloader_coverage_workflow_evidence_capture_plan"] = mortgage_data.get("mortgage_downloader_coverage_workflow_evidence_capture_plan")
        report["mortgage_workflow_downloader_coverage_next_action_plan_count"] = mortgage_data.get("mortgage_downloader_coverage_next_action_plan_count")
        report["mortgage_workflow_downloader_coverage_next_action_manual_count"] = mortgage_data.get("mortgage_downloader_coverage_next_action_manual_count")
        report["mortgage_workflow_downloader_coverage_next_action_safe_to_run_automatically_count"] = mortgage_data.get("mortgage_downloader_coverage_next_action_safe_to_run_automatically_count")
        report["mortgage_workflow_downloader_coverage_next_action_plan"] = mortgage_data.get("mortgage_downloader_coverage_next_action_plan")
        report["mortgage_workflow_downloader_coverage_safe_to_run_automatically"] = mortgage_data.get("mortgage_downloader_coverage_safe_to_run_automatically")
        report["mortgage_workflow_evidence_advance_status"] = mortgage_data.get("mortgage_workflow_evidence_advance_status")
        report["mortgage_workflow_evidence_advance_reason"] = mortgage_data.get("mortgage_workflow_evidence_advance_reason")
        report["mortgage_workflow_evidence_advance_property_count"] = mortgage_data.get("mortgage_workflow_evidence_advance_property_count")
        report["mortgage_workflow_evidence_advance_ok_count"] = mortgage_data.get("mortgage_workflow_evidence_advance_ok_count")
        report["mortgage_workflow_evidence_advance_review_count"] = mortgage_data.get("mortgage_workflow_evidence_advance_review_count")
        report["mortgage_workflow_evidence_advance_safe_to_run_automatically"] = mortgage_data.get("mortgage_workflow_evidence_advance_safe_to_run_automatically")
        report["mortgage_workflow_evidence_advance_apply_evidence"] = mortgage_data.get("mortgage_workflow_evidence_advance_apply_evidence")
        report["mortgage_workflow_evidence_advance_write_stubs"] = mortgage_data.get("mortgage_workflow_evidence_advance_write_stubs")
        report["mortgage_workflow_evidence_advance_install_registry_entries"] = mortgage_data.get("mortgage_workflow_evidence_advance_install_registry_entries")
        report["mortgage_workflow_evidence_advance_apply_registry"] = mortgage_data.get("mortgage_workflow_evidence_advance_apply_registry")
        report["mortgage_workflow_evidence_advance_enable_verified_registry_entry"] = mortgage_data.get("mortgage_workflow_evidence_advance_enable_verified_registry_entry")
        report["mortgage_workflow_evidence_advance_results"] = mortgage_data.get("mortgage_workflow_evidence_advance_results")
        report["mortgage_workflow_evidence_refresh_status"] = mortgage_data.get("mortgage_workflow_evidence_refresh_status")
        report["mortgage_workflow_evidence_refresh_idempotency_digest"] = mortgage_data.get("mortgage_workflow_evidence_refresh_idempotency_digest")
        report["mortgage_workflow_evidence_refresh_reason"] = mortgage_data.get("mortgage_workflow_evidence_refresh_reason")
        report["mortgage_workflow_evidence_refresh_property_count"] = mortgage_data.get("mortgage_workflow_evidence_refresh_property_count")
        report["mortgage_workflow_evidence_refresh_analysis_ready_count"] = mortgage_data.get("mortgage_workflow_evidence_refresh_analysis_ready_count")
        report["mortgage_workflow_evidence_refresh_analysis_blocked_count"] = mortgage_data.get("mortgage_workflow_evidence_refresh_analysis_blocked_count")
        report["mortgage_workflow_evidence_refresh_scaffold_ready_count"] = mortgage_data.get("mortgage_workflow_evidence_refresh_scaffold_ready_count")
        report["mortgage_workflow_evidence_refresh_ensure_handoffs"] = mortgage_data.get("mortgage_workflow_evidence_refresh_ensure_handoffs")
        report["mortgage_workflow_evidence_refresh_handoff_pending_property_count"] = mortgage_data.get("mortgage_workflow_evidence_refresh_handoff_pending_property_count")
        report["mortgage_workflow_evidence_refresh_handoff_pending_properties"] = mortgage_data.get("mortgage_workflow_evidence_refresh_handoff_pending_properties")
        report["mortgage_workflow_evidence_refresh_handoff_file_gap_count"] = mortgage_data.get("mortgage_workflow_evidence_refresh_handoff_file_gap_count")
        report["mortgage_workflow_evidence_refresh_handoff_file_gap_properties"] = mortgage_data.get("mortgage_workflow_evidence_refresh_handoff_file_gap_properties")
        report["mortgage_workflow_evidence_refresh_har_path_missing_count"] = mortgage_data.get("mortgage_workflow_evidence_refresh_har_path_missing_count")
        report["mortgage_workflow_evidence_refresh_har_path_missing_properties"] = mortgage_data.get("mortgage_workflow_evidence_refresh_har_path_missing_properties")
        report["mortgage_workflow_evidence_refresh_har_path_present_blocked_count"] = mortgage_data.get("mortgage_workflow_evidence_refresh_har_path_present_blocked_count")
        report["mortgage_workflow_evidence_refresh_har_path_present_blocked_properties"] = mortgage_data.get("mortgage_workflow_evidence_refresh_har_path_present_blocked_properties")
        report["mortgage_workflow_evidence_refresh_post_capture_check_ready_count"] = mortgage_data.get("mortgage_workflow_evidence_refresh_post_capture_check_ready_count")
        report["mortgage_workflow_evidence_refresh_post_capture_check_ready_properties"] = mortgage_data.get("mortgage_workflow_evidence_refresh_post_capture_check_ready_properties")
        report["mortgage_workflow_evidence_refresh_capture_required_before_offline_next_step_count"] = mortgage_data.get("mortgage_workflow_evidence_refresh_capture_required_before_offline_next_step_count")
        report["mortgage_workflow_evidence_refresh_capture_required_before_offline_next_step_properties"] = mortgage_data.get("mortgage_workflow_evidence_refresh_capture_required_before_offline_next_step_properties")
        report["mortgage_workflow_evidence_refresh_offline_next_step_ready_count"] = mortgage_data.get("mortgage_workflow_evidence_refresh_offline_next_step_ready_count")
        report["mortgage_workflow_evidence_refresh_offline_next_step_gap_count"] = mortgage_data.get("mortgage_workflow_evidence_refresh_offline_next_step_gap_count")
        report["mortgage_workflow_evidence_refresh_offline_next_step_gap_properties"] = mortgage_data.get("mortgage_workflow_evidence_refresh_offline_next_step_gap_properties")
        report["mortgage_workflow_evidence_refresh_raw_discovered_har_candidate_property_count"] = mortgage_data.get("mortgage_workflow_evidence_refresh_raw_discovered_har_candidate_property_count")
        report["mortgage_workflow_evidence_refresh_raw_discovered_har_candidate_properties"] = mortgage_data.get("mortgage_workflow_evidence_refresh_raw_discovered_har_candidate_properties")
        report["mortgage_workflow_evidence_refresh_discovered_har_candidate_property_count"] = mortgage_data.get("mortgage_workflow_evidence_refresh_discovered_har_candidate_property_count")
        report["mortgage_workflow_evidence_refresh_discovered_har_candidate_properties"] = mortgage_data.get("mortgage_workflow_evidence_refresh_discovered_har_candidate_properties")
        report["mortgage_workflow_evidence_refresh_safe_discovered_har_candidate_property_count"] = mortgage_data.get("mortgage_workflow_evidence_refresh_safe_discovered_har_candidate_property_count")
        report["mortgage_workflow_evidence_refresh_safe_discovered_har_candidate_properties"] = mortgage_data.get("mortgage_workflow_evidence_refresh_safe_discovered_har_candidate_properties")
        report["mortgage_workflow_evidence_refresh_selected_discovered_har_candidate_count"] = mortgage_data.get("mortgage_workflow_evidence_refresh_selected_discovered_har_candidate_count")
        report["mortgage_workflow_evidence_refresh_selected_discovered_har_candidate_properties"] = mortgage_data.get("mortgage_workflow_evidence_refresh_selected_discovered_har_candidate_properties")
        report["mortgage_workflow_evidence_refresh_rejected_discovered_har_candidate_count"] = mortgage_data.get("mortgage_workflow_evidence_refresh_rejected_discovered_har_candidate_count")
        report["mortgage_workflow_evidence_refresh_rejected_discovered_har_candidate_paths"] = mortgage_data.get("mortgage_workflow_evidence_refresh_rejected_discovered_har_candidate_paths")
        report["mortgage_workflow_evidence_refresh_next_action_plan_count"] = mortgage_data.get("mortgage_workflow_evidence_refresh_next_action_plan_count")
        report["mortgage_workflow_evidence_refresh_next_action_manual_count"] = mortgage_data.get("mortgage_workflow_evidence_refresh_next_action_manual_count")
        report["mortgage_workflow_evidence_refresh_next_action_safe_to_run_automatically_count"] = mortgage_data.get("mortgage_workflow_evidence_refresh_next_action_safe_to_run_automatically_count")
        report["mortgage_workflow_evidence_refresh_safe_to_run_automatically"] = mortgage_data.get("mortgage_workflow_evidence_refresh_safe_to_run_automatically")
        report["mortgage_workflow_evidence_refresh_next_action_plan"] = mortgage_data.get("mortgage_workflow_evidence_refresh_next_action_plan")
        report["mortgage_workflow_replayability_status"] = mortgage_data.get("mortgage_workflow_replayability_status")
        report["mortgage_workflow_replayability_reason"] = mortgage_data.get("mortgage_workflow_replayability_reason")
        report["mortgage_workflow_replayability_safe_to_run_automatically"] = mortgage_data.get("mortgage_workflow_replayability_safe_to_run_automatically")
        report["mortgage_workflow_replayability_har_analysis_count"] = mortgage_data.get("mortgage_workflow_replayability_har_analysis_count")
        report["mortgage_workflow_replayability_har_analysis_ready_count"] = mortgage_data.get("mortgage_workflow_replayability_har_analysis_ready_count")
        report["mortgage_workflow_replayability_har_analysis_blocked_count"] = mortgage_data.get("mortgage_workflow_replayability_har_analysis_blocked_count")
        report["mortgage_workflow_replayability_raw_discovered_har_candidate_property_count"] = mortgage_data.get("mortgage_workflow_replayability_raw_discovered_har_candidate_property_count")
        report["mortgage_workflow_replayability_raw_discovered_har_candidate_properties"] = mortgage_data.get("mortgage_workflow_replayability_raw_discovered_har_candidate_properties")
        report["mortgage_workflow_replayability_discovered_har_candidate_property_count"] = mortgage_data.get("mortgage_workflow_replayability_discovered_har_candidate_property_count")
        report["mortgage_workflow_replayability_discovered_har_candidate_properties"] = mortgage_data.get("mortgage_workflow_replayability_discovered_har_candidate_properties")
        report["mortgage_workflow_replayability_safe_discovered_har_candidate_property_count"] = mortgage_data.get("mortgage_workflow_replayability_safe_discovered_har_candidate_property_count")
        report["mortgage_workflow_replayability_safe_discovered_har_candidate_properties"] = mortgage_data.get("mortgage_workflow_replayability_safe_discovered_har_candidate_properties")
        report["mortgage_workflow_replayability_selected_discovered_har_candidate_count"] = mortgage_data.get("mortgage_workflow_replayability_selected_discovered_har_candidate_count")
        report["mortgage_workflow_replayability_selected_discovered_har_candidate_properties"] = mortgage_data.get("mortgage_workflow_replayability_selected_discovered_har_candidate_properties")
        report["mortgage_workflow_replayability_rejected_discovered_har_candidate_count"] = mortgage_data.get("mortgage_workflow_replayability_rejected_discovered_har_candidate_count")
        report["mortgage_workflow_replayability_rejected_discovered_har_candidate_paths"] = mortgage_data.get("mortgage_workflow_replayability_rejected_discovered_har_candidate_paths")
        report["mortgage_workflow_replayability_next_action_plan_count"] = mortgage_data.get("mortgage_workflow_replayability_next_action_plan_count")
        report["mortgage_workflow_replayability_next_action_manual_count"] = mortgage_data.get("mortgage_workflow_replayability_next_action_manual_count")
        report["mortgage_workflow_replayability_next_action_safe_to_run_automatically_count"] = mortgage_data.get("mortgage_workflow_replayability_next_action_safe_to_run_automatically_count")
        report["mortgage_workflow_replayability_next_action_plan"] = mortgage_data.get("mortgage_workflow_replayability_next_action_plan")
        report["mortgage_workflow_replayability_citadel_status"] = mortgage_data.get("mortgage_workflow_replayability_citadel_status")
        report["mortgage_workflow_replayability_citadel_ready"] = mortgage_data.get("mortgage_workflow_replayability_citadel_ready")
        report["mortgage_workflow_replayability_citadel_blocker"] = mortgage_data.get("mortgage_workflow_replayability_citadel_blocker")
        report["mortgage_workflow_replayability_citadel_target_month"] = mortgage_data.get("mortgage_workflow_replayability_citadel_target_month")
        report["mortgage_workflow_replayability_citadel_target_month_replayable_document_available"] = mortgage_data.get("mortgage_workflow_replayability_citadel_target_month_replayable_document_available")
        report["mortgage_workflow_replayability_citadel_target_month_replayable_document_payload_count"] = mortgage_data.get("mortgage_workflow_replayability_citadel_target_month_replayable_document_payload_count")
        report["mortgage_workflow_replayability_citadel_replayable_statement_months"] = mortgage_data.get("mortgage_workflow_replayability_citadel_replayable_statement_months")
        report["mortgage_workflow_replayability_citadel_statement_document_months"] = mortgage_data.get("mortgage_workflow_replayability_citadel_statement_document_months")
        report["mortgage_workflow_replayability_citadel_direct_pdf_response_count"] = mortgage_data.get("mortgage_workflow_replayability_citadel_direct_pdf_response_count")
        report["mortgage_workflow_replayability_citadel_direct_pdf_missing_response_count"] = mortgage_data.get("mortgage_workflow_replayability_citadel_direct_pdf_missing_response_count")
        report["mortgage_workflow_replayability_citadel_direct_pdf_missing_response_paths"] = mortgage_data.get("mortgage_workflow_replayability_citadel_direct_pdf_missing_response_paths")
        report["mortgage_workflow_replayability_citadel_missing_response_body_count"] = mortgage_data.get("mortgage_workflow_replayability_citadel_missing_response_body_count")
        report["mortgage_workflow_replayability_citadel_missing_response_body_path_counts"] = mortgage_data.get("mortgage_workflow_replayability_citadel_missing_response_body_path_counts")
        report["mortgage_workflow_capture_queue_rc"] = mortgage_data.get("mortgage_workflow_capture_queue_rc")
        report["mortgage_workflow_capture_queue_report"] = mortgage_data.get("mortgage_workflow_capture_queue_report")
        report["mortgage_workflow_capture_queue_markdown"] = mortgage_data.get("mortgage_workflow_capture_queue_markdown")
        report["mortgage_workflow_capture_queue_idempotency_digest"] = mortgage_data.get("mortgage_workflow_capture_queue_idempotency_digest")
        report["mortgage_workflow_capture_queue_status"] = mortgage_data.get("mortgage_workflow_capture_queue_status")
        report["mortgage_workflow_capture_queue_reason"] = mortgage_data.get("mortgage_workflow_capture_queue_reason")
        report["mortgage_workflow_capture_queue_candidate_capture_handoff_count"] = mortgage_data.get("mortgage_workflow_capture_queue_candidate_capture_handoff_count")
        report["mortgage_workflow_capture_queue_downloader_gap_candidate_capture_count"] = mortgage_data.get("mortgage_workflow_capture_queue_downloader_gap_candidate_capture_count")
        report["mortgage_workflow_capture_queue_downloader_gap_candidate_capture_properties"] = mortgage_data.get("mortgage_workflow_capture_queue_downloader_gap_candidate_capture_properties")
        report["mortgage_workflow_capture_queue_capture_count"] = mortgage_data.get("mortgage_workflow_capture_queue_capture_count")
        report["mortgage_workflow_capture_queue_capture_properties"] = mortgage_data.get("mortgage_workflow_capture_queue_capture_properties")
        report["mortgage_workflow_capture_queue_duplicate_capture_handoff_count"] = mortgage_data.get("mortgage_workflow_capture_queue_duplicate_capture_handoff_count")
        report["mortgage_workflow_capture_queue_duplicate_capture_handoff_properties"] = mortgage_data.get("mortgage_workflow_capture_queue_duplicate_capture_handoff_properties")
        report["mortgage_workflow_capture_queue_duplicate_capture_handoff_identities"] = mortgage_data.get("mortgage_workflow_capture_queue_duplicate_capture_handoff_identities")
        report["mortgage_workflow_capture_queue_duplicate_capture_handoffs_suppressed"] = mortgage_data.get("mortgage_workflow_capture_queue_duplicate_capture_handoffs_suppressed")
        report["mortgage_workflow_capture_queue_missing_har_count"] = mortgage_data.get("mortgage_workflow_capture_queue_missing_har_count")
        report["mortgage_workflow_capture_queue_missing_har_properties"] = mortgage_data.get("mortgage_workflow_capture_queue_missing_har_properties")
        report["mortgage_workflow_capture_queue_existing_har_needs_bodies_count"] = mortgage_data.get("mortgage_workflow_capture_queue_existing_har_needs_bodies_count")
        report["mortgage_workflow_capture_queue_existing_har_needs_bodies_properties"] = mortgage_data.get("mortgage_workflow_capture_queue_existing_har_needs_bodies_properties")
        report["mortgage_workflow_capture_queue_existing_har_needs_target_month_statement_pdf_count"] = mortgage_data.get("mortgage_workflow_capture_queue_existing_har_needs_target_month_statement_pdf_count")
        report["mortgage_workflow_capture_queue_existing_har_needs_target_month_statement_pdf_properties"] = mortgage_data.get("mortgage_workflow_capture_queue_existing_har_needs_target_month_statement_pdf_properties")
        report["mortgage_workflow_capture_queue_rejected_discovered_har_candidate_count"] = mortgage_data.get("mortgage_workflow_capture_queue_rejected_discovered_har_candidate_count")
        report["mortgage_workflow_capture_queue_rejected_discovered_har_candidate_paths"] = mortgage_data.get("mortgage_workflow_capture_queue_rejected_discovered_har_candidate_paths")
        report["mortgage_workflow_capture_queue_entries"] = mortgage_data.get("mortgage_workflow_capture_queue_entries")
        report["mortgage_workflow_capture_queue_safe_to_run_automatically"] = mortgage_data.get("mortgage_workflow_capture_queue_safe_to_run_automatically")
        report["mortgage_workflow_capture_queue_manual_capture_count"] = mortgage_data.get("mortgage_workflow_capture_queue_manual_capture_count")
        report["mortgage_workflow_capture_queue_manual_capture_properties"] = mortgage_data.get("mortgage_workflow_capture_queue_manual_capture_properties")
        report["mortgage_workflow_capture_queue_automatic_capture_count"] = mortgage_data.get("mortgage_workflow_capture_queue_automatic_capture_count")
        report["mortgage_workflow_capture_queue_automatic_capture_properties"] = mortgage_data.get("mortgage_workflow_capture_queue_automatic_capture_properties")
        report["mortgage_workflow_capture_queue_tokenomics_blocking_count"] = mortgage_data.get("mortgage_workflow_capture_queue_tokenomics_blocking_count")
        report["mortgage_workflow_capture_queue_tokenomics_blocking_properties"] = mortgage_data.get("mortgage_workflow_capture_queue_tokenomics_blocking_properties")
        report["mortgage_workflow_capture_queue_tokenomics_non_blocking_count"] = mortgage_data.get("mortgage_workflow_capture_queue_tokenomics_non_blocking_count")
        report["mortgage_workflow_capture_queue_tokenomics_non_blocking_properties"] = mortgage_data.get("mortgage_workflow_capture_queue_tokenomics_non_blocking_properties")
        report["mortgage_workflow_capture_queue_tokenomics_workbook_write_blocker_count"] = mortgage_data.get("mortgage_workflow_capture_queue_tokenomics_workbook_write_blocker_count")
        report["mortgage_workflow_capture_queue_tokenomics_workbook_write_blocker_properties"] = mortgage_data.get("mortgage_workflow_capture_queue_tokenomics_workbook_write_blocker_properties")
        report["mortgage_workflow_capture_queue_tokenomics_workbook_write_blocker_reasons"] = mortgage_data.get("mortgage_workflow_capture_queue_tokenomics_workbook_write_blocker_reasons")
        report["mortgage_workflow_capture_queue_processor_rc"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_rc")
        report["mortgage_workflow_capture_queue_processor_report"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_report")
        report["mortgage_workflow_capture_queue_processor_markdown"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_markdown")
        report["mortgage_workflow_capture_queue_processor_idempotency_digest"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_idempotency_digest")
        report["mortgage_workflow_capture_queue_processor_status"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_status")
        report["mortgage_workflow_capture_queue_processor_reason"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_reason")
        report["mortgage_workflow_capture_queue_processor_apply"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_apply")
        report["mortgage_workflow_capture_queue_processor_process_existing_body_recaptures"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_process_existing_body_recaptures")
        report["mortgage_workflow_capture_queue_processor_enable_verified_registry_entry"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_enable_verified_registry_entry")
        report["mortgage_workflow_capture_queue_processor_entry_count"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_entry_count")
        report["mortgage_workflow_capture_queue_processor_processed_count"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_processed_count")
        report["mortgage_workflow_capture_queue_processor_applied_count"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_applied_count")
        report["mortgage_workflow_capture_queue_processor_dry_run_processed_count"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_dry_run_processed_count")
        report["mortgage_workflow_capture_queue_processor_pending_apply_count"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_pending_apply_count")
        report["mortgage_workflow_capture_queue_processor_pending_apply_properties"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_pending_apply_properties")
        report["mortgage_workflow_capture_queue_processor_durable_completion_count"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_durable_completion_count")
        report["mortgage_workflow_capture_queue_processor_durable_completion_properties"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_durable_completion_properties")
        report["mortgage_workflow_capture_queue_processor_ok_count"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_ok_count")
        report["mortgage_workflow_capture_queue_processor_review_count"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_review_count")
        report["mortgage_workflow_capture_queue_processor_skipped_count"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_skipped_count")
        report["mortgage_workflow_capture_queue_processor_missing_har_count"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_missing_har_count")
        report["mortgage_workflow_capture_queue_processor_missing_har_properties"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_missing_har_properties")
        report["mortgage_workflow_capture_queue_processor_invalid_har_count"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_invalid_har_count")
        report["mortgage_workflow_capture_queue_processor_invalid_har_properties"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_invalid_har_properties")
        report["mortgage_workflow_capture_queue_processor_auth_blocked_before_capture_count"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_auth_blocked_before_capture_count")
        report["mortgage_workflow_capture_queue_processor_auth_blocked_before_capture_properties"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_auth_blocked_before_capture_properties")
        report["mortgage_workflow_capture_queue_processor_auth_blocked_capture_requirements"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_auth_blocked_capture_requirements")
        report["mortgage_workflow_capture_queue_processor_existing_har_recapture_skipped_count"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_existing_har_recapture_skipped_count")
        report["mortgage_workflow_capture_queue_processor_existing_har_recapture_skipped_properties"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_existing_har_recapture_skipped_properties")
        report["mortgage_workflow_capture_queue_processor_existing_har_recapture_requirements"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_existing_har_recapture_requirements")
        report["mortgage_workflow_capture_queue_processor_existing_har_target_month_pdf_recapture_skipped_count"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_existing_har_target_month_pdf_recapture_skipped_count")
        report["mortgage_workflow_capture_queue_processor_existing_har_target_month_pdf_recapture_skipped_properties"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_existing_har_target_month_pdf_recapture_skipped_properties")
        report["mortgage_workflow_capture_queue_processor_existing_har_target_month_pdf_recapture_requirements"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_existing_har_target_month_pdf_recapture_requirements")
        report["mortgage_workflow_capture_queue_processor_existing_body_recapture_skipped_count"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_existing_body_recapture_skipped_count")
        report["mortgage_workflow_capture_queue_processor_existing_body_recapture_skipped_properties"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_existing_body_recapture_skipped_properties")
        report["mortgage_workflow_capture_queue_processor_existing_body_recapture_requirements"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_existing_body_recapture_requirements")
        report["mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocker_count"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocker_count")
        report["mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocker_properties"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocker_properties")
        report["mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocker_reasons"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocker_reasons")
        report["mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocker_statement_month_statuses"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocker_statement_month_statuses")
        report["mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocking_count"] = mortgage_field(
            "mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocking_count",
            "mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocker_count",
        )
        report["mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocking_properties"] = mortgage_field(
            "mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocking_properties",
            "mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocker_properties",
        )
        report["mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocking_reasons"] = mortgage_field(
            "mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocking_reasons",
            "mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocker_reasons",
        )
        report["mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocking_statement_month_statuses"] = mortgage_field(
            "mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocking_statement_month_statuses",
            "mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocker_statement_month_statuses",
        )
        report["mortgage_workflow_capture_queue_processor_tokenomics_queue_blocker_count"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_tokenomics_queue_blocker_count")
        report["mortgage_workflow_capture_queue_processor_tokenomics_queue_blocker_properties"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_tokenomics_queue_blocker_properties")
        report["mortgage_workflow_capture_queue_processor_queue_blocker_status"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_queue_blocker_status")
        report["mortgage_workflow_capture_queue_processor_queue_blocker_reason"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_queue_blocker_reason")
        report["mortgage_workflow_capture_queue_processor_queue_blocker_count"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_queue_blocker_count")
        report["mortgage_workflow_capture_queue_processor_queue_blocker_properties"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_queue_blocker_properties")
        report["mortgage_workflow_capture_queue_processor_operator_next_action_count"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_operator_next_action_count")
        report["mortgage_workflow_capture_queue_processor_operator_next_action_properties"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_operator_next_action_properties")
        report["mortgage_workflow_capture_queue_processor_operator_next_actions"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_operator_next_actions")
        report["mortgage_workflow_capture_queue_processor_live_cdp_downloader_handoff_count"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_live_cdp_downloader_handoff_count")
        report["mortgage_workflow_capture_queue_processor_live_cdp_downloader_handoffs"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_live_cdp_downloader_handoffs")
        report["mortgage_workflow_capture_queue_processor_live_cdp_downloader_properties"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_live_cdp_downloader_properties")
        report["mortgage_workflow_capture_queue_processor_live_cdp_downloader_commands"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_live_cdp_downloader_commands")
        report["mortgage_workflow_capture_queue_processor_safe_to_run_automatically"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_safe_to_run_automatically")
        report["mortgage_workflow_credential_repair_action_count"] = mortgage_data.get("mortgage_workflow_credential_repair_action_count")
        report["mortgage_workflow_credential_repair_properties"] = mortgage_data.get("mortgage_workflow_credential_repair_properties")
        report["mortgage_workflow_credential_repair_actions"] = mortgage_data.get("mortgage_workflow_credential_repair_actions")
        report["mortgage_workflow_portal_auth_repair_action_count"] = mortgage_data.get("mortgage_workflow_portal_auth_repair_action_count")
        report["mortgage_workflow_portal_auth_repair_properties"] = mortgage_data.get("mortgage_workflow_portal_auth_repair_properties")
        report["mortgage_workflow_portal_auth_repair_actions"] = mortgage_data.get("mortgage_workflow_portal_auth_repair_actions")
        report["mortgage_workflow_replayability_credential_repair_action_count"] = mortgage_data.get("mortgage_workflow_replayability_credential_repair_action_count")
        report["mortgage_workflow_replayability_credential_repair_properties"] = mortgage_data.get("mortgage_workflow_replayability_credential_repair_properties")
        report["mortgage_workflow_replayability_credential_repair_actions"] = mortgage_data.get("mortgage_workflow_replayability_credential_repair_actions")
        report["mortgage_workflow_replayability_portal_auth_repair_action_count"] = mortgage_data.get("mortgage_workflow_replayability_portal_auth_repair_action_count")
        report["mortgage_workflow_replayability_portal_auth_repair_properties"] = mortgage_data.get("mortgage_workflow_replayability_portal_auth_repair_properties")
        report["mortgage_workflow_replayability_portal_auth_repair_actions"] = mortgage_data.get("mortgage_workflow_replayability_portal_auth_repair_actions")
        report["mortgage_workflow_capture_queue_processor_credential_repair_action_count"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_credential_repair_action_count")
        report["mortgage_workflow_capture_queue_processor_credential_repair_properties"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_credential_repair_properties")
        report["mortgage_workflow_capture_queue_processor_credential_repair_actions"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_credential_repair_actions")
        report["mortgage_workflow_capture_queue_processor_portal_auth_repair_action_count"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_portal_auth_repair_action_count")
        report["mortgage_workflow_capture_queue_processor_portal_auth_repair_properties"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_portal_auth_repair_properties")
        report["mortgage_workflow_capture_queue_processor_portal_auth_repair_actions"] = mortgage_data.get("mortgage_workflow_capture_queue_processor_portal_auth_repair_actions")
        capture_queue_entries = compact_rows(report.get("mortgage_workflow_capture_queue_entries"))
        processor_next_actions = compact_rows(report.get("mortgage_workflow_capture_queue_processor_operator_next_actions"))
        credential_diagnostic_fields = (
            "credentials_available",
            "credential_source",
            "manual_auth_reason",
            "credential_lookup_uri_host_aliases",
            "credential_lookup_search_terms",
            "credential_lookup_candidate_search_term_count",
            "credential_lookup_candidate_search_terms",
            "credential_lookup_candidate_count",
            "credential_lookup_candidate_items",
            "credential_lookup_unguarded_candidate_count",
            "credential_lookup_unguarded_candidate_items",
            "credential_lookup_misfiled_candidate_count",
            "credential_lookup_absent",
            "credential_lookup_scope",
            "credential_lookup_repair_action",
            "credential_login_hint_mismatch",
            "credential_login_hint_mismatch_overridden",
        )
        processor_next_actions_by_property = {
            item_property(item): item
            for item in processor_next_actions
            if item_property(item)
        }
        enriched_capture_queue_entries = []
        for item in capture_queue_entries:
            enriched_item = dict(item)
            processor_source = processor_next_actions_by_property.get(item_property(enriched_item))
            if isinstance(processor_source, dict):
                for field in credential_diagnostic_fields:
                    if (
                        (field not in enriched_item or enriched_item.get(field) is None)
                        and field in processor_source
                    ):
                        enriched_item[field] = processor_source.get(field)
                if (
                    not enriched_item.get("command_precondition_next_action")
                    and processor_source.get("credential_lookup_repair_action")
                ):
                    enriched_item["command_precondition_next_action"] = processor_source.get(
                        "credential_lookup_repair_action"
                    )
            enriched_capture_queue_entries.append(enriched_item)
        capture_queue_entries = enriched_capture_queue_entries
        report["mortgage_workflow_capture_queue_entries"] = capture_queue_entries
        if not isinstance(report.get("mortgage_workflow_capture_queue_actions"), list):
            report["mortgage_workflow_capture_queue_actions"] = capture_queue_entries
        report["mortgage_workflow_capture_queue_command_precondition_statuses"] = unique_row_values(
            capture_queue_entries, "command_precondition_status"
        )
        report["mortgage_workflow_capture_queue_command_precondition_reasons"] = unique_row_values(
            capture_queue_entries, "command_precondition_reason"
        )
        report["mortgage_workflow_capture_queue_command_precondition_next_actions"] = unique_row_values(
            capture_queue_entries, "command_precondition_next_action"
        )
        report["mortgage_workflow_capture_queue_command_precondition_properties"] = unique_row_properties(
            capture_queue_entries, "command_precondition_status"
        )
        report["mortgage_workflow_capture_queue_blocked_by_auth_properties"] = unique_row_properties(
            capture_queue_entries, "command_precondition_status", "blocked_by_auth"
        )
        report["mortgage_workflow_capture_queue_missing_workflow_har_properties"] = unique_row_properties(
            capture_queue_entries, "command_precondition_status", "missing_workflow_har"
        )
        report["mortgage_workflow_capture_queue_duplicate_target_statement_months"] = unique_row_values(
            capture_queue_entries, "duplicate_target_statement_months"
        )
        report["mortgage_workflow_capture_queue_target_month_recapture_reasons"] = unique_row_values(
            capture_queue_entries, "target_month_recapture_reason"
        )
        report["mortgage_workflow_capture_queue_target_month_recapture_properties"] = unique_row_properties(
            capture_queue_entries, "target_month_recapture_reason"
        )
        report["mortgage_workflow_capture_queue_target_month_candidate_pdf_payload_statuses"] = candidate_values(
            capture_queue_entries, "pdf_payload_status"
        )
        report["mortgage_workflow_capture_queue_target_month_candidate_document_ids"] = candidate_values(
            capture_queue_entries, "document_identifier"
        )
        report["mortgage_workflow_capture_queue_credential_lookup_statuses"] = unique_row_values(
            capture_queue_entries, "credential_lookup_status"
        )
        report["mortgage_workflow_capture_queue_credential_lookup_failure_reasons"] = unique_row_values(
            capture_queue_entries, "credential_lookup_failure_reason"
        )
        report["mortgage_workflow_capture_queue_credential_lookup_item_names"] = unique_row_values(
            capture_queue_entries, "credential_lookup_item_name"
        )
        report["mortgage_workflow_capture_queue_credential_lookup_expected_folder_names"] = unique_row_values(
            capture_queue_entries, "credential_lookup_expected_folder_name"
        )
        report["mortgage_workflow_capture_queue_credential_lookup_uri_hosts"] = unique_row_values(
            capture_queue_entries, "credential_lookup_uri_host"
        )
        report["mortgage_workflow_capture_queue_credential_login_hint_mismatch_values"] = unique_row_values(
            capture_queue_entries, "credential_login_hint_mismatch"
        )
        report["mortgage_workflow_capture_queue_credential_login_hint_mismatch_properties"] = unique_row_properties(
            capture_queue_entries, "credential_login_hint_mismatch", True
        )
        report["mortgage_workflow_capture_queue_credential_login_hint_mismatch_overridden_values"] = unique_row_values(
            capture_queue_entries, "credential_login_hint_mismatch_overridden"
        )
        report["mortgage_workflow_capture_queue_credential_login_hint_mismatch_overridden_properties"] = unique_row_properties(
            capture_queue_entries, "credential_login_hint_mismatch_overridden", True
        )
        report["mortgage_workflow_capture_queue_credential_lookup_candidate_counts"] = unique_row_values(
            capture_queue_entries, "credential_lookup_candidate_count"
        )
        report["mortgage_workflow_capture_queue_credential_lookup_unguarded_candidate_counts"] = unique_row_values(
            capture_queue_entries, "credential_lookup_unguarded_candidate_count"
        )
        report["mortgage_workflow_capture_queue_credential_lookup_misfiled_candidate_counts"] = unique_row_values(
            capture_queue_entries, "credential_lookup_misfiled_candidate_count"
        )
        report["mortgage_workflow_capture_queue_credential_lookup_absent_values"] = unique_row_values(
            capture_queue_entries, "credential_lookup_absent"
        )
        report["mortgage_workflow_capture_queue_credential_lookup_scopes"] = unique_row_values(
            capture_queue_entries, "credential_lookup_scope"
        )
        report["mortgage_workflow_capture_queue_credential_lookup_repair_actions"] = unique_row_values(
            capture_queue_entries, "credential_lookup_repair_action"
        )
        report["mortgage_workflow_capture_queue_credential_lookup_candidate_items"] = [
            {
                "property": item_property(item),
                "credential_lookup_item_name": item.get("credential_lookup_item_name"),
                "credential_lookup_candidate_count": item.get("credential_lookup_candidate_count"),
                "credential_lookup_candidate_items": item.get("credential_lookup_candidate_items"),
            }
            for item in capture_queue_entries
            if item.get("credential_lookup_candidate_items")
        ]
        if isinstance(report.get("mortgage_workflow_credential_repair_actions"), list):
            capture_queue_entries_by_property = {
                item_property(item): item
                for item in capture_queue_entries
                if item_property(item)
            }
            enriched_credential_repair_actions = []
            for repair in report.get("mortgage_workflow_credential_repair_actions"):
                if not isinstance(repair, dict):
                    continue
                enriched_repair = dict(repair)
                source = capture_queue_entries_by_property.get(item_property(enriched_repair))
                if isinstance(source, dict):
                    for field in credential_diagnostic_fields:
                        if (
                            (field not in enriched_repair or enriched_repair.get(field) is None)
                            and field in source
                        ):
                            enriched_repair[field] = source.get(field)
                    if (
                        source.get("credential_lookup_repair_action")
                        and enriched_repair.get("credential_repair_action") == "create_or_move_bitwarden_item"
                    ):
                        enriched_repair["credential_repair_action"] = source.get("credential_lookup_repair_action")
                if (
                    enriched_repair.get("credential_repair_action") == "update_bitwarden_login_hint_or_force_auto_login"
                    and enriched_repair.get("credential_login_hint_mismatch") is True
                    and enriched_repair.get("credential_login_hint_mismatch_overridden") is True
                ):
                    continue
                enriched_credential_repair_actions.append(enriched_repair)
            report["mortgage_workflow_credential_repair_actions"] = enriched_credential_repair_actions
        if not isinstance(report.get("mortgage_workflow_credential_repair_actions"), list):
            credential_repair_actions = []
            seen_credential_repairs = set()
            target_month = report.get("mortgage_workflow_mortgage_statement_target_month") or report.get(
                "mortgage_statement_target_month"
            )
            for item in capture_queue_entries:
                repair_action = None
                if (
                    item.get("credential_login_hint_mismatch") is True
                    and item.get("credential_login_hint_mismatch_overridden") is not True
                ):
                    repair_action = "update_bitwarden_login_hint_or_force_auto_login"
                elif item.get("credential_lookup_status") == "failed":
                    if item.get("credential_lookup_repair_action"):
                        repair_action = item.get("credential_lookup_repair_action")
                    elif item.get("credential_lookup_failure_reason") == "item_not_found":
                        if item.get("credential_lookup_absent") is True:
                            repair_action = "create_bitwarden_item"
                        elif (
                            isinstance(item.get("credential_lookup_misfiled_candidate_count"), int)
                            and item.get("credential_lookup_misfiled_candidate_count") > 0
                        ):
                            repair_action = "move_existing_bitwarden_item_or_update_expected_folder"
                        else:
                            repair_action = "create_or_move_bitwarden_item"
                    else:
                        repair_action = "repair_bitwarden_credential_lookup"
                elif (
                    item.get("command_precondition_status") == "blocked_by_auth"
                    and item.get("credential_login_hint_mismatch") is not True
                ):
                    repair_action = "repair_portal_auth"
                if not repair_action:
                    continue
                credentials_available = item.get("credentials_available")
                if credentials_available is None and item.get("credential_lookup_status") == "failed":
                    credentials_available = False
                credential_source = item.get("credential_source")
                if credential_source in (None, "", [], {}) and credentials_available is False:
                    credential_source = "unavailable"
                manual_auth_reason = item.get("manual_auth_reason")
                if manual_auth_reason in (None, "", [], {}) and credentials_available is False:
                    credential_source_text = " ".join(
                        str(item.get(key) or "")
                        for key in (
                            "source_downloader_id",
                            "credential_lookup_uri_host",
                            "credential_lookup_item_name",
                            "servicer_hint",
                            "servicer",
                        )
                    ).lower()
                    manual_auth_reason = (
                        "loandepot_credentials_unavailable"
                        if "loandepot" in credential_source_text
                        else "credentials_unavailable"
                    )
                downloader_id = item.get("source_downloader_id")
                validation_command = item.get("validation_command")
                if downloader_id:
                    validation_command = (
                        "MORTGAGE_DOWNLOADER_PROFILE=credential_check "
                        f"MORTGAGE_DOWNLOADER_IDS={downloader_id} "
                        f"MORTGAGE_STATEMENT_TARGET_MONTH={target_month} "
                        "python3 scripts/run_mortgage_statement_downloaders.py"
                    )
                repair = {
                    "property": item_property(item),
                    "reason": item.get("reason"),
                    "command_precondition_status": item.get("command_precondition_status"),
                    "command_precondition_reason": item.get("command_precondition_reason"),
                    "credentials_available": credentials_available,
                    "credential_source": credential_source,
                    "manual_auth_reason": manual_auth_reason,
                    "credential_lookup_status": item.get("credential_lookup_status"),
                    "credential_lookup_failure_reason": item.get("credential_lookup_failure_reason"),
                    "credential_lookup_item_name": item.get("credential_lookup_item_name"),
                    "credential_lookup_expected_folder_name": item.get("credential_lookup_expected_folder_name"),
                    "credential_lookup_uri_host": item.get("credential_lookup_uri_host"),
                    "credential_lookup_uri_host_aliases": item.get("credential_lookup_uri_host_aliases"),
                    "credential_lookup_search_terms": item.get("credential_lookup_search_terms"),
                    "credential_lookup_candidate_search_term_count": item.get("credential_lookup_candidate_search_term_count"),
                    "credential_lookup_candidate_search_terms": item.get("credential_lookup_candidate_search_terms"),
                    "credential_lookup_candidate_count": item.get("credential_lookup_candidate_count"),
                    "credential_lookup_candidate_items": item.get("credential_lookup_candidate_items"),
                    "credential_lookup_unguarded_candidate_count": item.get("credential_lookup_unguarded_candidate_count"),
                    "credential_lookup_unguarded_candidate_items": item.get("credential_lookup_unguarded_candidate_items"),
                    "credential_lookup_misfiled_candidate_count": item.get("credential_lookup_misfiled_candidate_count"),
                    "credential_lookup_absent": item.get("credential_lookup_absent"),
                    "credential_lookup_scope": item.get("credential_lookup_scope"),
                    "credential_lookup_repair_action": item.get("credential_lookup_repair_action"),
                    "credential_login_hint_mismatch": item.get("credential_login_hint_mismatch"),
                    "credential_login_hint_mismatch_overridden": item.get(
                        "credential_login_hint_mismatch_overridden"
                    ),
                    "credential_repair_action": repair_action,
                    "credential_repair_validation_command": validation_command,
                    "operator_next_action": item.get("source_downloader_operator_next_action")
                    or item.get("source_downloader_suggested_next_action")
                    or item.get("command_precondition_next_action"),
                    "source_downloader_id": downloader_id,
                }
                repair = {
                    key: value
                    for key, value in repair.items()
                    if value not in (None, "", {})
                    and (
                        value != []
                        or key
                        in {
                            "credential_lookup_candidate_items",
                            "credential_lookup_unguarded_candidate_items",
                        }
                    )
                }
                marker = json.dumps(
                    {
                        "property": repair.get("property"),
                        "credential_repair_action": repair.get("credential_repair_action"),
                        "credential_lookup_item_name": repair.get("credential_lookup_item_name"),
                    },
                    sort_keys=True,
                )
                if marker in seen_credential_repairs:
                    continue
                seen_credential_repairs.add(marker)
                credential_repair_actions.append(repair)
            report["mortgage_workflow_credential_repair_actions"] = credential_repair_actions
        credential_repair_actions = report.get("mortgage_workflow_credential_repair_actions")
        if isinstance(credential_repair_actions, list):
            report["mortgage_workflow_credential_repair_action_count"] = len(credential_repair_actions)
            report["mortgage_workflow_credential_repair_properties"] = unique_row_values(
                credential_repair_actions,
                "property",
            )
        if not isinstance(
            report.get("mortgage_workflow_downloader_coverage_automation_repair_not_statement_gap_downloaders"),
            list,
        ):
            repair_not_gap_downloaders = []
            seen_repair_not_gap = set()
            for item in report.get("mortgage_workflow_downloader_coverage_blocked_configured_downloaders") or []:
                if not isinstance(item, dict):
                    continue
                not_statement_gap = (
                    item.get("automation_repair_priority") == "automation_repair_not_statement_gap"
                    or (
                        item.get("current_month_statement_available") is True
                        and item.get("statement_coverage_satisfied") is True
                        and item.get("tokenomics_statement_gap_blocking") is False
                    )
                )
                if not not_statement_gap:
                    continue
                marker = json.dumps(
                    {
                        "downloader_id": item.get("downloader_id"),
                        "property": item.get("property"),
                    },
                    sort_keys=True,
                )
                if marker in seen_repair_not_gap:
                    continue
                seen_repair_not_gap.add(marker)
                repair_not_gap_downloaders.append(item)
            report["mortgage_workflow_downloader_coverage_automation_repair_not_statement_gap_downloaders"] = repair_not_gap_downloaders
            report["mortgage_workflow_downloader_coverage_automation_repair_not_statement_gap_count"] = len(
                repair_not_gap_downloaders
            )
            report["mortgage_workflow_downloader_coverage_automation_repair_not_statement_gap_properties"] = unique_row_values(
                repair_not_gap_downloaders,
                "property",
            )
        live_cdp_queue_entries = [
            item for item in capture_queue_entries if item.get("live_cdp_downloader_command")
        ]
        report["mortgage_workflow_capture_queue_live_cdp_downloader_handoff_count"] = len(live_cdp_queue_entries)
        report["mortgage_workflow_capture_queue_live_cdp_downloader_properties"] = unique_values(
            item_property(item) for item in live_cdp_queue_entries
        )
        report["mortgage_workflow_capture_queue_live_cdp_downloader_commands"] = unique_row_values(
            live_cdp_queue_entries, "live_cdp_downloader_command"
        )
        report["mortgage_workflow_capture_queue_live_cdp_downloader_handoffs"] = [
            {
                "property": item_property(item),
                "live_cdp_downloader_command": item.get("live_cdp_downloader_command"),
                "live_cdp_downloader_ready_after_visible_auth": item.get(
                    "live_cdp_downloader_ready_after_visible_auth"
                ),
                "capture_auth_blocker": item.get("capture_auth_blocker"),
                "capture_auth_blocker_reason": item.get("capture_auth_blocker_reason"),
                "capture_command_blocked_reason": item.get("capture_command_blocked_reason"),
                "credential_lookup_status": item.get("credential_lookup_status"),
                "credential_lookup_failure_reason": item.get("credential_lookup_failure_reason"),
                "credential_lookup_item_name": item.get("credential_lookup_item_name"),
                "credential_lookup_candidate_count": item.get("credential_lookup_candidate_count"),
                "credential_lookup_candidate_items": item.get("credential_lookup_candidate_items"),
                "source_downloader_operator_next_action": item.get("source_downloader_operator_next_action"),
            }
            for item in live_cdp_queue_entries
        ]
        report["mortgage_workflow_capture_queue_processor_command_precondition_statuses"] = unique_row_values(
            processor_next_actions, "command_precondition_status"
        )
        report["mortgage_workflow_capture_queue_processor_command_precondition_reasons"] = unique_row_values(
            processor_next_actions, "command_precondition_reason"
        )
        report["mortgage_workflow_capture_queue_processor_command_precondition_next_actions"] = unique_row_values(
            processor_next_actions, "command_precondition_next_action"
        )
        report["mortgage_workflow_capture_queue_processor_command_precondition_properties"] = unique_row_properties(
            processor_next_actions, "command_precondition_status"
        )
        report["mortgage_workflow_capture_queue_processor_blocked_by_auth_properties"] = unique_row_properties(
            processor_next_actions, "command_precondition_status", "blocked_by_auth"
        )
        report["mortgage_workflow_capture_queue_processor_missing_workflow_har_properties"] = unique_row_properties(
            processor_next_actions, "command_precondition_status", "missing_workflow_har"
        )
        report["mortgage_workflow_capture_queue_processor_target_month_recapture_reasons"] = unique_row_values(
            processor_next_actions, "target_month_recapture_reason"
        )
        report["mortgage_workflow_capture_queue_processor_target_month_recapture_properties"] = unique_row_properties(
            processor_next_actions, "target_month_recapture_reason"
        )
        report["mortgage_workflow_capture_queue_processor_target_month_candidate_pdf_payload_statuses"] = candidate_values(
            processor_next_actions, "pdf_payload_status"
        )
        report["mortgage_workflow_capture_queue_processor_target_month_candidate_document_ids"] = candidate_values(
            processor_next_actions, "document_identifier"
        )
        report["mortgage_workflow_capture_queue_processor_credential_lookup_statuses"] = unique_row_values(
            processor_next_actions, "credential_lookup_status"
        )
        report["mortgage_workflow_capture_queue_processor_credential_lookup_failure_reasons"] = unique_row_values(
            processor_next_actions, "credential_lookup_failure_reason"
        )
        report["mortgage_workflow_capture_queue_processor_credential_lookup_item_names"] = unique_row_values(
            processor_next_actions, "credential_lookup_item_name"
        )
        report["mortgage_workflow_capture_queue_processor_credential_lookup_expected_folder_names"] = unique_row_values(
            processor_next_actions, "credential_lookup_expected_folder_name"
        )
        report["mortgage_workflow_capture_queue_processor_credential_lookup_uri_hosts"] = unique_row_values(
            processor_next_actions, "credential_lookup_uri_host"
        )
        report["mortgage_workflow_capture_queue_processor_credential_login_hint_mismatch_values"] = unique_row_values(
            processor_next_actions, "credential_login_hint_mismatch"
        )
        report["mortgage_workflow_capture_queue_processor_credential_login_hint_mismatch_properties"] = unique_row_properties(
            processor_next_actions, "credential_login_hint_mismatch", True
        )
        report["mortgage_workflow_capture_queue_processor_credential_login_hint_mismatch_overridden_values"] = unique_row_values(
            processor_next_actions, "credential_login_hint_mismatch_overridden"
        )
        report["mortgage_workflow_capture_queue_processor_credential_login_hint_mismatch_overridden_properties"] = unique_row_properties(
            processor_next_actions, "credential_login_hint_mismatch_overridden", True
        )
        report["mortgage_workflow_capture_queue_processor_credential_lookup_candidate_counts"] = unique_row_values(
            processor_next_actions, "credential_lookup_candidate_count"
        )
        report["mortgage_workflow_capture_queue_processor_credential_lookup_candidate_items"] = [
            {
                "property": item_property(item),
                "credential_lookup_item_name": item.get("credential_lookup_item_name"),
                "credential_lookup_candidate_count": item.get("credential_lookup_candidate_count"),
                "credential_lookup_candidate_items": item.get("credential_lookup_candidate_items"),
            }
            for item in processor_next_actions
            if item.get("credential_lookup_candidate_items")
        ]
        report["mortgage_workflow_registry_handoff_count"] = mortgage_data.get("mortgage_workflow_registry_handoff_count")
        report["mortgage_workflow_registry_handoffs"] = mortgage_data.get("mortgage_workflow_registry_handoffs")
        report["mortgage_workflow_registry_handoff_paths"] = mortgage_data.get("mortgage_workflow_registry_handoff_paths")
        report["mortgage_workflow_registry_install_commands"] = mortgage_data.get("mortgage_workflow_registry_install_commands")
        report["mortgage_workflow_registry_safe_to_register_automatically_count"] = mortgage_data.get("mortgage_workflow_registry_safe_to_register_automatically_count")
        report["mortgage_workflow_property_matrix_count"] = mortgage_data.get("mortgage_workflow_property_matrix_count")
        report["mortgage_workflow_property_matrix_review_count"] = mortgage_data.get("mortgage_workflow_property_matrix_review_count")
        report["mortgage_workflow_property_matrix_manual_action_count"] = mortgage_data.get("mortgage_workflow_property_matrix_manual_action_count")
        report["mortgage_workflow_property_matrix_safe_action_count"] = mortgage_data.get("mortgage_workflow_property_matrix_safe_action_count")
        report["mortgage_workflow_property_matrix"] = mortgage_data.get("mortgage_workflow_property_matrix")
        report["mortgage_workflow_automation_blocked_with_current_statement_count"] = mortgage_data.get("mortgage_workflow_automation_blocked_with_current_statement_count")
        report["mortgage_workflow_automation_blocked_with_current_statement_properties"] = mortgage_data.get("mortgage_workflow_automation_blocked_with_current_statement_properties")
        report["mortgage_workflow_automation_blocked_with_current_statement_records"] = mortgage_data.get("mortgage_workflow_automation_blocked_with_current_statement_records")
        report["mortgage_workflow_current_cycle_future_automation_attention_required"] = mortgage_data.get("mortgage_workflow_current_cycle_future_automation_attention_required")
        report["mortgage_workflow_current_cycle_future_automation_attention_count"] = mortgage_data.get("mortgage_workflow_current_cycle_future_automation_attention_count")
        report["mortgage_workflow_current_cycle_future_automation_attention_properties"] = mortgage_data.get("mortgage_workflow_current_cycle_future_automation_attention_properties")
        report["mortgage_workflow_current_cycle_future_automation_attention_reasons"] = mortgage_data.get("mortgage_workflow_current_cycle_future_automation_attention_reasons")
        report["mortgage_workflow_current_cycle_future_automation_attention_records"] = mortgage_data.get("mortgage_workflow_current_cycle_future_automation_attention_records")
        report["mortgage_workflow_future_automation_repair_action_count"] = mortgage_data.get("mortgage_workflow_future_automation_repair_action_count")
        report["mortgage_workflow_future_automation_repair_properties"] = mortgage_data.get("mortgage_workflow_future_automation_repair_properties")
        report["mortgage_workflow_future_automation_repair_reasons"] = mortgage_data.get("mortgage_workflow_future_automation_repair_reasons")
        report["mortgage_workflow_future_automation_repair_actions"] = mortgage_data.get("mortgage_workflow_future_automation_repair_actions")
        report["mortgage_workflow_tokenomics_workbook_write_blocking_property_count"] = mortgage_field(
            "tokenomics_workbook_write_blocking_property_count",
            "tokenomics_workbook_write_blocker_count",
        )
        report["mortgage_workflow_tokenomics_workbook_write_blocking_properties"] = mortgage_field(
            "tokenomics_workbook_write_blocking_properties",
            "tokenomics_workbook_write_blocker_properties",
        )
        report["mortgage_workflow_tokenomics_workbook_write_blocking_reasons"] = mortgage_field(
            "tokenomics_workbook_write_blocking_reasons",
            "tokenomics_workbook_write_blocker_reasons",
        )
        report["mortgage_workflow_tokenomics_workbook_write_blocking_statement_month_statuses"] = mortgage_field(
            "tokenomics_workbook_write_blocking_statement_month_statuses",
            "tokenomics_workbook_write_blocker_statement_month_statuses",
        )
        report["mortgage_workflow_coownership_tokenomics_status"] = mortgage_data.get("coownership_tokenomics_status")
        report["mortgage_workflow_coownership_tokenomics_effective_status"] = mortgage_data.get("coownership_tokenomics_effective_status")
        report["mortgage_workflow_coownership_tokenomics_current_month"] = mortgage_data.get("coownership_tokenomics_current_month")
        report["mortgage_workflow_coownership_tokenomics_workbook_write_ready"] = mortgage_data.get("coownership_tokenomics_workbook_write_ready")
        report["mortgage_workflow_coownership_tokenomics_workbook_write_gate_status"] = mortgage_data.get("coownership_tokenomics_workbook_write_gate_status")
        report["mortgage_workflow_coownership_tokenomics_workbook_write_blocker_count"] = mortgage_data.get("coownership_tokenomics_workbook_write_blocker_count")
        report["mortgage_workflow_coownership_tokenomics_workbook_write_blocker_properties"] = mortgage_data.get("coownership_tokenomics_workbook_write_blocker_properties")
        report["mortgage_workflow_coownership_tokenomics_workbook_write_blocker_reasons"] = mortgage_data.get("coownership_tokenomics_workbook_write_blocker_reasons")
        report["mortgage_workflow_coownership_tokenomics_workbook_write_blockers"] = mortgage_data.get("coownership_tokenomics_workbook_write_blockers")
        report["mortgage_workflow_coownership_tokenomics_workbook_write_blocking_property_count"] = mortgage_field(
            "coownership_tokenomics_workbook_write_blocking_property_count",
            "coownership_tokenomics_workbook_write_blocker_count",
        )
        report["mortgage_workflow_coownership_tokenomics_workbook_write_blocking_properties"] = mortgage_field(
            "coownership_tokenomics_workbook_write_blocking_properties",
            "coownership_tokenomics_workbook_write_blocker_properties",
        )
        report["mortgage_workflow_coownership_tokenomics_workbook_write_ready_property_count"] = mortgage_data.get("coownership_tokenomics_workbook_write_ready_property_count")
        report["mortgage_workflow_coownership_tokenomics_workbook_write_ready_properties"] = mortgage_data.get("coownership_tokenomics_workbook_write_ready_properties")
        report["mortgage_workflow_coownership_tokenomics_workbook_ready_property_write_plan_count"] = mortgage_data.get("coownership_tokenomics_workbook_ready_property_write_plan_count")
        report["mortgage_workflow_coownership_tokenomics_workbook_ready_property_write_plan_properties"] = mortgage_data.get("coownership_tokenomics_workbook_ready_property_write_plan_properties")
        report["mortgage_workflow_coownership_tokenomics_workbook_ready_property_write_plan"] = mortgage_data.get("coownership_tokenomics_workbook_ready_property_write_plan")
        report["mortgage_workflow_coownership_tokenomics_workbook_ready_property_planned_cell_write_count"] = mortgage_data.get("coownership_tokenomics_workbook_ready_property_planned_cell_write_count")
        report["mortgage_workflow_coownership_tokenomics_workbook_ready_property_write_blocked_by_full_gate"] = mortgage_data.get("coownership_tokenomics_workbook_ready_property_write_blocked_by_full_gate")
        report["mortgage_workflow_coownership_tokenomics_workbook_partial_ready_property_write_attempted"] = mortgage_field("coownership_tokenomics_workbook_partial_ready_property_write_attempted", "tokenomics_workbook_partial_ready_property_write_attempted")
        report["mortgage_workflow_coownership_tokenomics_workbook_partial_ready_property_write_applied"] = mortgage_field("coownership_tokenomics_workbook_partial_ready_property_write_applied", "tokenomics_workbook_partial_ready_property_write_applied")
        report["mortgage_workflow_coownership_tokenomics_workbook_partial_ready_property_write_reason"] = mortgage_field("coownership_tokenomics_workbook_partial_ready_property_write_reason", "tokenomics_workbook_partial_ready_property_write_reason")
        report["mortgage_workflow_coownership_tokenomics_workbook_partial_ready_property_write_property_count"] = mortgage_field("coownership_tokenomics_workbook_partial_ready_property_write_property_count", "tokenomics_workbook_partial_ready_property_write_property_count")
        report["mortgage_workflow_coownership_tokenomics_workbook_partial_ready_property_write_properties"] = mortgage_field("coownership_tokenomics_workbook_partial_ready_property_write_properties", "tokenomics_workbook_partial_ready_property_write_properties")
        report["mortgage_workflow_coownership_tokenomics_workbook_partial_ready_property_cleared_copied_stale_property_count"] = mortgage_field("coownership_tokenomics_workbook_partial_ready_property_cleared_copied_stale_property_count", "tokenomics_workbook_partial_ready_property_cleared_copied_stale_property_count")
        report["mortgage_workflow_coownership_tokenomics_workbook_partial_ready_property_cleared_copied_stale_properties"] = mortgage_field("coownership_tokenomics_workbook_partial_ready_property_cleared_copied_stale_properties", "tokenomics_workbook_partial_ready_property_cleared_copied_stale_properties")
        report["mortgage_workflow_coownership_tokenomics_blocking_capture_queue_count"] = mortgage_data.get("coownership_tokenomics_blocking_capture_queue_count")
        report["mortgage_workflow_coownership_tokenomics_blocking_capture_queue_properties"] = mortgage_data.get("coownership_tokenomics_blocking_capture_queue_properties")
        report["mortgage_workflow_coownership_tokenomics_blocking_capture_queue"] = mortgage_data.get("coownership_tokenomics_blocking_capture_queue")
        report["mortgage_workflow_review_packet"] = mortgage_data.get("review_packet") or str(mortgage_workflow_review_packet)
        report["mortgage_downloader_report"] = mortgage_data.get("mortgage_downloader_report")
        report["mortgage_downloader_citadel_auth_preflight_report"] = mortgage_data.get("mortgage_downloader_citadel_auth_preflight_report")
        report["mortgage_downloader_citadel_auth_preflight_rc"] = mortgage_data.get("mortgage_downloader_citadel_auth_preflight_rc")
        report["mortgage_downloader_citadel_auth_preflight_status"] = mortgage_data.get("mortgage_downloader_citadel_auth_preflight_status")
        report["mortgage_downloader_citadel_auth_preflight_reason"] = mortgage_data.get("mortgage_downloader_citadel_auth_preflight_reason")
        report["mortgage_downloader_citadel_auth_preflight_manual_auth_required"] = mortgage_data.get("mortgage_downloader_citadel_auth_preflight_manual_auth_required")
        report["mortgage_downloader_citadel_auth_preflight_candidate_count"] = mortgage_data.get("mortgage_downloader_citadel_auth_preflight_candidate_count")
        report["mortgage_downloader_citadel_auth_preflight_route_counts"] = mortgage_data.get("mortgage_downloader_citadel_auth_preflight_route_counts")
        report["mortgage_downloader_citadel_auth_preflight_login_tab_count"] = mortgage_data.get("mortgage_downloader_citadel_auth_preflight_login_tab_count")
        report["mortgage_downloader_citadel_auth_preflight_non_login_tab_count"] = mortgage_data.get("mortgage_downloader_citadel_auth_preflight_non_login_tab_count")
        report["mortgage_downloader_citadel_auth_preflight_scanned_count"] = mortgage_data.get("mortgage_downloader_citadel_auth_preflight_scanned_count")
        report["mortgage_downloader_citadel_auth_preflight_authenticated_found"] = mortgage_data.get("mortgage_downloader_citadel_auth_preflight_authenticated_found")
        report["mortgage_downloader_citadel_har_preflight_report"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_report")
        report["mortgage_downloader_citadel_har_preflight_rc"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_rc")
        report["mortgage_downloader_citadel_har_preflight_status"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_status")
        report["mortgage_downloader_citadel_har_preflight_capture_quality_status"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_capture_quality_status")
        report["mortgage_downloader_citadel_har_preflight_replay_blocker"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_replay_blocker")
        report["mortgage_downloader_citadel_har_preflight_missing_response_body_count"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_missing_response_body_count")
        report["mortgage_downloader_citadel_har_preflight_missing_response_body_paths"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_missing_response_body_paths")
        report["mortgage_downloader_citadel_har_preflight_missing_response_body_path_counts"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_missing_response_body_path_counts")
        report["mortgage_downloader_citadel_har_preflight_response_body_requirements"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_response_body_requirements")
        report["mortgage_downloader_citadel_har_preflight_replayable_document_payload_count"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_replayable_document_payload_count")
        report["mortgage_downloader_citadel_har_preflight_target_month"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_target_month")
        report["mortgage_downloader_citadel_har_preflight_target_month_replayable_document_available"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_target_month_replayable_document_available")
        report["mortgage_downloader_citadel_har_preflight_target_month_replayable_document_payload_count"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_target_month_replayable_document_payload_count")
        report["mortgage_downloader_citadel_har_preflight_replayable_statement_months"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_replayable_statement_months")
        report["mortgage_downloader_citadel_har_preflight_statement_document_months"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_statement_document_months")
        report["mortgage_downloader_citadel_har_preflight_direct_pdf_response_count"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_direct_pdf_response_count")
        report["mortgage_downloader_citadel_har_preflight_source_direct_pdf_candidate_count"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_source_direct_pdf_candidate_count")
        report["mortgage_downloader_citadel_har_preflight_source_direct_pdf_path_counts"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_source_direct_pdf_path_counts")
        report["mortgage_downloader_citadel_har_preflight_source_direct_pdf_filenames"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_source_direct_pdf_filenames")
        report["mortgage_downloader_citadel_har_preflight_source_direct_pdf_filename_candidates"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_source_direct_pdf_filename_candidates")
        report["mortgage_downloader_citadel_har_preflight_target_month_direct_pdf_filenames"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_target_month_direct_pdf_filenames")
        report["mortgage_downloader_citadel_har_preflight_target_month_direct_pdf_filename_candidates"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_target_month_direct_pdf_filename_candidates")
        report["mortgage_downloader_citadel_har_preflight_target_month_direct_pdf_body_missing_candidate_count"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_target_month_direct_pdf_body_missing_candidate_count")
        report["mortgage_downloader_citadel_har_preflight_source_required_response_candidate_count"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_source_required_response_candidate_count")
        report["mortgage_downloader_citadel_har_preflight_source_required_response_path_counts"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_source_required_response_path_counts")
        report["mortgage_downloader_citadel_har_preflight_direct_pdf_missing_response_count"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_direct_pdf_missing_response_count")
        report["mortgage_downloader_citadel_har_preflight_direct_pdf_missing_response_paths"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_direct_pdf_missing_response_paths")
        report["mortgage_downloader_citadel_har_preflight_replayable_json_response_count"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_replayable_json_response_count")
        report["mortgage_downloader_citadel_har_preflight_embedded_response_body_count"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_embedded_response_body_count")
        report["mortgage_downloader_citadel_har_preflight_embedded_access_token_count"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_embedded_access_token_count")
        report["mortgage_downloader_citadel_har_preflight_can_replay_documents"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_can_replay_documents")
        report["mortgage_downloader_citadel_har_preflight_next_action_status"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_next_action_status")
        report["mortgage_downloader_citadel_har_preflight_next_action_reason"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_next_action_reason")
        report["mortgage_downloader_citadel_har_preflight_next_action_command"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_next_action_command")
        report["mortgage_downloader_citadel_har_preflight_next_action_capture_command"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_next_action_capture_command")
        report["mortgage_downloader_citadel_har_preflight_next_action_capture_required"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_next_action_capture_required")
        report["mortgage_downloader_citadel_har_preflight_next_action_target_month"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_next_action_target_month")
        report["mortgage_downloader_citadel_har_preflight_next_action_target_month_replayable_document_available"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_next_action_target_month_replayable_document_available")
        report["mortgage_downloader_citadel_har_preflight_next_action_target_month_replayable_document_payload_count"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_next_action_target_month_replayable_document_payload_count")
        report["mortgage_downloader_citadel_har_preflight_next_action_replayable_statement_months"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_next_action_replayable_statement_months")
        report["mortgage_downloader_citadel_har_preflight_next_action_statement_document_months"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_next_action_statement_document_months")
        report["mortgage_downloader_citadel_har_preflight_next_action_required_response_paths"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_next_action_required_response_paths")
        report["mortgage_downloader_citadel_har_preflight_next_action_response_body_requirements"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_next_action_response_body_requirements")
        report["mortgage_downloader_citadel_har_preflight_next_action_source_direct_pdf_filenames"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_next_action_source_direct_pdf_filenames")
        report["mortgage_downloader_citadel_har_preflight_next_action_target_month_direct_pdf_filenames"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_next_action_target_month_direct_pdf_filenames")
        report["mortgage_downloader_citadel_har_preflight_next_action_target_month_direct_pdf_body_missing_candidate_count"] = mortgage_data.get("mortgage_downloader_citadel_har_preflight_next_action_target_month_direct_pdf_body_missing_candidate_count")
        report["mortgage_downloader_citadel_capture_plan_report"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_report")
        report["mortgage_downloader_citadel_capture_plan_markdown"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_markdown")
        report["mortgage_downloader_citadel_capture_plan_status"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_status")
        report["mortgage_downloader_citadel_capture_plan_latest_live_auth_report"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_latest_live_auth_report")
        report["mortgage_downloader_citadel_capture_plan_latest_live_auth_status"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_latest_live_auth_status")
        report["mortgage_downloader_citadel_capture_plan_latest_live_auth_blocker"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_latest_live_auth_blocker")
        report["mortgage_downloader_citadel_capture_plan_latest_live_auth_auth_failure_reason"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_latest_live_auth_auth_failure_reason")
        report["mortgage_downloader_citadel_capture_plan_latest_live_auth_auth_failure_visible_reason"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_latest_live_auth_auth_failure_visible_reason")
        report["mortgage_downloader_citadel_capture_plan_latest_live_auth_manual_auth_required"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_latest_live_auth_manual_auth_required")
        report["mortgage_downloader_citadel_capture_plan_latest_live_auth_manual_auth_file"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_latest_live_auth_manual_auth_file")
        report["mortgage_downloader_citadel_capture_plan_latest_live_auth_oauth_password_grant_error_codes"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_latest_live_auth_oauth_password_grant_error_codes")
        report["mortgage_downloader_citadel_capture_plan_capture_precondition_status"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_capture_precondition_status")
        report["mortgage_downloader_citadel_capture_plan_capture_precondition_blocker"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_capture_precondition_blocker")
        report["mortgage_downloader_citadel_capture_plan_capture_precondition_reason"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_capture_precondition_reason")
        report["mortgage_downloader_citadel_capture_plan_capture_precondition_manual_auth_required"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_capture_precondition_manual_auth_required")
        report["mortgage_downloader_citadel_capture_plan_capture_precondition_next_action"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_capture_precondition_next_action")
        report["mortgage_downloader_citadel_capture_plan_capture_precondition_safe_to_capture_now"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_capture_precondition_safe_to_capture_now")
        report["mortgage_downloader_citadel_capture_plan_capture_precondition_prepare_command"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_capture_precondition_prepare_command")
        report["mortgage_downloader_citadel_capture_plan_capture_precondition_safe_to_prepare_now"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_capture_precondition_safe_to_prepare_now")
        report["mortgage_downloader_citadel_capture_plan_target_month"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_target_month")
        report["mortgage_downloader_citadel_capture_plan_target_month_replayable_document_available"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_target_month_replayable_document_available")
        report["mortgage_downloader_citadel_capture_plan_target_month_replayable_document_payload_count"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_target_month_replayable_document_payload_count")
        report["mortgage_downloader_citadel_capture_plan_replayable_statement_months"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_replayable_statement_months")
        report["mortgage_downloader_citadel_capture_plan_statement_document_months"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_statement_document_months")
        report["mortgage_downloader_citadel_capture_plan_capture_required"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_capture_required")
        report["mortgage_downloader_citadel_capture_plan_capture_command"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_capture_command")
        report["mortgage_downloader_citadel_capture_plan_next_command"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_next_command")
        report["mortgage_downloader_citadel_capture_plan_capture_har_path"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_capture_har_path")
        report["mortgage_downloader_citadel_capture_plan_post_capture_diagnostics_command"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_post_capture_diagnostics_command")
        report["mortgage_downloader_citadel_capture_plan_post_capture_check_script_path"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_post_capture_check_script_path")
        report["mortgage_downloader_citadel_capture_plan_post_capture_check_command"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_post_capture_check_command")
        report["mortgage_downloader_citadel_capture_plan_post_capture_check_safe_to_run_after_har_capture"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_post_capture_check_safe_to_run_after_har_capture")
        report["mortgage_downloader_citadel_capture_plan_validate_existing_capture_command"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_validate_existing_capture_command")
        report["mortgage_downloader_citadel_capture_plan_advance_capture_workflow_evidence_dry_run_command"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_advance_capture_workflow_evidence_dry_run_command")
        report["mortgage_downloader_citadel_capture_plan_advance_capture_workflow_evidence_apply_command"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_advance_capture_workflow_evidence_apply_command")
        report["mortgage_downloader_citadel_capture_plan_workflow_evidence_advance_report"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_workflow_evidence_advance_report")
        report["mortgage_downloader_citadel_capture_plan_advance_workflow_evidence_command"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_advance_workflow_evidence_command")
        report["mortgage_downloader_citadel_capture_plan_advance_workflow_evidence_apply_command"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_advance_workflow_evidence_apply_command")
        report["mortgage_downloader_citadel_capture_plan_required_response_paths"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_required_response_paths")
        report["mortgage_downloader_citadel_capture_plan_required_response_path_counts"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_required_response_path_counts")
        report["mortgage_downloader_citadel_capture_plan_response_body_requirements"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_response_body_requirements")
        report["mortgage_downloader_citadel_capture_plan_source_direct_pdf_candidate_count"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_source_direct_pdf_candidate_count")
        report["mortgage_downloader_citadel_capture_plan_source_direct_pdf_path_counts"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_source_direct_pdf_path_counts")
        report["mortgage_downloader_citadel_capture_plan_source_direct_pdf_filenames"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_source_direct_pdf_filenames")
        report["mortgage_downloader_citadel_capture_plan_source_direct_pdf_filename_candidates"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_source_direct_pdf_filename_candidates")
        report["mortgage_downloader_citadel_capture_plan_target_month_direct_pdf_filenames"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_target_month_direct_pdf_filenames")
        report["mortgage_downloader_citadel_capture_plan_target_month_direct_pdf_filename_candidates"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_target_month_direct_pdf_filename_candidates")
        report["mortgage_downloader_citadel_capture_plan_target_month_direct_pdf_body_missing_candidate_count"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_target_month_direct_pdf_body_missing_candidate_count")
        report["mortgage_downloader_citadel_capture_plan_source_required_response_candidate_count"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_source_required_response_candidate_count")
        report["mortgage_downloader_citadel_capture_plan_source_required_response_path_counts"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_source_required_response_path_counts")
        report["mortgage_downloader_citadel_capture_plan_safe_to_run_automatically"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_safe_to_run_automatically")
        report["mortgage_downloader_citadel_capture_plan_raw_source_urls_included"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_raw_source_urls_included")
        report["mortgage_downloader_citadel_capture_plan_secrets_included"] = mortgage_data.get("mortgage_downloader_citadel_capture_plan_secrets_included")
        report["mortgage_downloader_status"] = mortgage_data.get("mortgage_downloader_status")
        report["mortgage_workflow_mortgage_downloader_status"] = mortgage_data.get("mortgage_downloader_status")
        report["mortgage_downloader_profile"] = mortgage_data.get("mortgage_downloader_profile")
        report["mortgage_workflow_mortgage_downloader_profile"] = mortgage_data.get("mortgage_downloader_profile")
        mortgage_downloader_target_month = mortgage_data.get("mortgage_downloader_target_month")
        if mortgage_downloader_target_month is None:
            mortgage_downloader_target_month = mortgage_data.get("mortgage_downloader_effective_statement_target_month")
        if mortgage_downloader_target_month is None:
            expected_months = mortgage_data.get("mortgage_downloader_expected_target_months")
            if isinstance(expected_months, list) and len(expected_months) == 1:
                mortgage_downloader_target_month = expected_months[0]
        report["mortgage_downloader_target_month"] = mortgage_downloader_target_month
        report["mortgage_workflow_mortgage_downloader_target_month"] = mortgage_data.get("mortgage_downloader_target_month")
        if report["mortgage_workflow_mortgage_downloader_target_month"] is None:
            report["mortgage_workflow_mortgage_downloader_target_month"] = mortgage_downloader_target_month
        mortgage_downloader_statement_target_month = (
            mortgage_data.get("mortgage_downloader_statement_target_month")
            or mortgage_data.get("mortgage_downloader_effective_statement_target_month")
        )
        if mortgage_downloader_statement_target_month is None:
            expected_months = mortgage_data.get("mortgage_downloader_expected_target_months")
            if isinstance(expected_months, list) and len(expected_months) == 1:
                mortgage_downloader_statement_target_month = expected_months[0]
        if mortgage_downloader_statement_target_month is None:
            mortgage_downloader_statement_target_month = mortgage_downloader_target_month
        report["mortgage_downloader_statement_target_month"] = mortgage_downloader_statement_target_month
        report["mortgage_workflow_mortgage_downloader_statement_target_month"] = mortgage_downloader_statement_target_month
        report["mortgage_downloader_enabled_count"] = mortgage_data.get("mortgage_downloader_enabled_count")
        report["mortgage_workflow_mortgage_downloader_enabled_count"] = mortgage_data.get("mortgage_downloader_enabled_count")
        report["mortgage_downloader_eligible_count"] = mortgage_data.get("mortgage_downloader_eligible_count")
        report["mortgage_workflow_mortgage_downloader_eligible_count"] = mortgage_data.get("mortgage_downloader_eligible_count")
        report["mortgage_downloader_skipped_non_co_owner_paid_count"] = mortgage_data.get("mortgage_downloader_skipped_non_co_owner_paid_count")
        report["mortgage_workflow_mortgage_downloader_skipped_non_co_owner_paid_count"] = mortgage_data.get("mortgage_downloader_skipped_non_co_owner_paid_count")
        report["mortgage_downloader_failed_count"] = mortgage_data.get("mortgage_downloader_failed_count")
        report["mortgage_workflow_mortgage_downloader_failed_count"] = mortgage_data.get("mortgage_downloader_failed_count")
        report["mortgage_downloader_expected_target_months"] = mortgage_data.get("mortgage_downloader_expected_target_months")
        report["mortgage_workflow_mortgage_downloader_expected_target_months"] = mortgage_data.get("mortgage_downloader_expected_target_months")
        report["mortgage_downloader_expected_target_month_count"] = mortgage_data.get("mortgage_downloader_expected_target_month_count")
        report["mortgage_workflow_mortgage_downloader_expected_target_month_count"] = mortgage_data.get("mortgage_downloader_expected_target_month_count")
        report["mortgage_downloader_effective_statement_target_month"] = mortgage_data.get("mortgage_downloader_effective_statement_target_month")
        report["mortgage_workflow_mortgage_downloader_effective_statement_target_month"] = mortgage_data.get("mortgage_downloader_effective_statement_target_month")
        report["mortgage_downloader_target_month_matches_all_expected_months"] = mortgage_data.get("mortgage_downloader_target_month_matches_all_expected_months")
        report["mortgage_workflow_mortgage_downloader_target_month_matches_all_expected_months"] = mortgage_data.get("mortgage_downloader_target_month_matches_all_expected_months")
        report["mortgage_downloader_target_month_differs_from_expected_months"] = mortgage_data.get("mortgage_downloader_target_month_differs_from_expected_months")
        report["mortgage_workflow_mortgage_downloader_target_month_differs_from_expected_months"] = mortgage_data.get("mortgage_downloader_target_month_differs_from_expected_months")
        report["mortgage_downloader_target_month_statement_available_count"] = mortgage_data.get("mortgage_downloader_target_month_statement_available_count")
        report["mortgage_downloader_target_month_statement_gap_count"] = mortgage_data.get("mortgage_downloader_target_month_statement_gap_count")
        report["mortgage_downloader_target_month_statement_gap_properties"] = mortgage_data.get("mortgage_downloader_target_month_statement_gap_properties")
        report["mortgage_downloader_target_month_statement_gaps"] = mortgage_data.get("mortgage_downloader_target_month_statement_gaps")
        report["mortgage_downloader_current_cycle_statement_ready"] = mortgage_data.get("mortgage_downloader_current_cycle_statement_ready")
        if report["mortgage_downloader_current_cycle_statement_ready"] is None:
            eligible_count = report.get("mortgage_downloader_eligible_count") or 0
            available_count = report.get("mortgage_downloader_target_month_statement_available_count") or 0
            gap_count = report.get("mortgage_downloader_target_month_statement_gap_count") or 0
            report["mortgage_downloader_current_cycle_statement_ready"] = (
                eligible_count > 0 and gap_count == 0 and available_count == eligible_count
            )
        report["mortgage_downloader_current_cycle_statement_ready_count"] = mortgage_data.get("mortgage_downloader_current_cycle_statement_ready_count")
        if report["mortgage_downloader_current_cycle_statement_ready_count"] is None:
            report["mortgage_downloader_current_cycle_statement_ready_count"] = report.get(
                "mortgage_downloader_target_month_statement_available_count",
                0,
            )
        report["mortgage_downloader_current_cycle_statement_blocker_count"] = mortgage_data.get("mortgage_downloader_current_cycle_statement_blocker_count")
        if report["mortgage_downloader_current_cycle_statement_blocker_count"] is None:
            report["mortgage_downloader_current_cycle_statement_blocker_count"] = report.get(
                "mortgage_downloader_target_month_statement_gap_count",
                0,
            )
        report["mortgage_downloader_current_cycle_statement_blocker_properties"] = mortgage_data.get("mortgage_downloader_current_cycle_statement_blocker_properties")
        if report["mortgage_downloader_current_cycle_statement_blocker_properties"] is None:
            report["mortgage_downloader_current_cycle_statement_blocker_properties"] = report.get(
                "mortgage_downloader_target_month_statement_gap_properties",
                [],
            )
        report["mortgage_downloader_current_cycle_future_automation_attention_required"] = mortgage_data.get("mortgage_downloader_current_cycle_future_automation_attention_required")
        if report["mortgage_downloader_current_cycle_future_automation_attention_required"] is None:
            report["mortgage_downloader_current_cycle_future_automation_attention_required"] = (
                (report.get("mortgage_downloader_automation_attention_count") or 0) > 0
            )
        report["mortgage_downloader_current_cycle_future_automation_attention_properties"] = mortgage_data.get("mortgage_downloader_current_cycle_future_automation_attention_properties")
        if report["mortgage_downloader_current_cycle_future_automation_attention_properties"] is None:
            report["mortgage_downloader_current_cycle_future_automation_attention_properties"] = report.get(
                "mortgage_downloader_automation_attention_properties",
                [],
            )
        report["mortgage_downloader_current_cycle_future_automation_attention_reasons"] = mortgage_data.get("mortgage_downloader_current_cycle_future_automation_attention_reasons")
        if report["mortgage_downloader_current_cycle_future_automation_attention_reasons"] is None:
            report["mortgage_downloader_current_cycle_future_automation_attention_reasons"] = report.get(
                "mortgage_downloader_automation_attention_reasons",
                [],
            )
        report["mortgage_downloader_failed_target_month_statement_available_count"] = mortgage_data.get("mortgage_downloader_failed_target_month_statement_available_count")
        report["mortgage_downloader_failed_target_month_statement_available_properties"] = mortgage_data.get("mortgage_downloader_failed_target_month_statement_available_properties")
        report["mortgage_downloader_failed_target_month_statement_available_details"] = mortgage_data.get("mortgage_downloader_failed_target_month_statement_available_details")
        report["mortgage_downloader_safe_to_run_automatically"] = mortgage_data.get("mortgage_downloader_safe_to_run_automatically")
        report["mortgage_downloader_safe_downloader_count"] = mortgage_data.get("mortgage_downloader_safe_downloader_count")
        report["mortgage_downloader_unsafe_downloader_count"] = mortgage_data.get("mortgage_downloader_unsafe_downloader_count")
        report["mortgage_downloader_unsafe_downloader_properties"] = mortgage_data.get("mortgage_downloader_unsafe_downloader_properties")
        report["mortgage_downloader_unsafe_downloader_details"] = mortgage_data.get("mortgage_downloader_unsafe_downloader_details")
        report["mortgage_downloader_automation_attention_count"] = mortgage_data.get("mortgage_downloader_automation_attention_count")
        report["mortgage_downloader_automation_attention_properties"] = mortgage_data.get("mortgage_downloader_automation_attention_properties")
        report["mortgage_downloader_automation_attention_reasons"] = mortgage_data.get("mortgage_downloader_automation_attention_reasons")
        report["mortgage_downloader_automation_attention_details"] = mortgage_data.get("mortgage_downloader_automation_attention_details")
        report["mortgage_downloader_results"] = mortgage_data.get("mortgage_downloader_results")
        report["mortgage_downloader_summaries"] = mortgage_data.get("mortgage_downloader_summaries")
        report["mortgage_downloader_status_counts"] = mortgage_data.get("mortgage_downloader_status_counts")
        for downloader_key in [
            "status",
            "profile",
            "enabled_count",
            "eligible_count",
            "skipped_non_co_owner_paid_count",
            "failed_count",
            "expected_target_months",
            "expected_target_month_count",
            "effective_statement_target_month",
            "target_month_matches_all_expected_months",
            "target_month_differs_from_expected_months",
            "target_month_statement_available_count",
            "target_month_statement_gap_count",
            "target_month_statement_gap_properties",
            "target_month_statement_gaps",
            "failed_target_month_statement_available_count",
            "failed_target_month_statement_available_properties",
            "failed_target_month_statement_available_details",
            "safe_to_run_automatically",
            "safe_downloader_count",
            "unsafe_downloader_count",
            "unsafe_downloader_properties",
            "unsafe_downloader_details",
            "automation_attention_count",
            "automation_attention_properties",
            "automation_attention_reasons",
            "automation_attention_details",
            "results",
            "summaries",
            "status_counts",
        ]:
            report[f"mortgage_workflow_mortgage_downloader_{downloader_key}"] = report.get(
                f"mortgage_downloader_{downloader_key}"
            )
        report["mortgage_downloader_citadel_prepare_rc"] = mortgage_data.get("mortgage_downloader_citadel_prepare_rc")
        report["mortgage_downloader_citadel_prepare_status"] = mortgage_data.get("mortgage_downloader_citadel_prepare_status")
        report["mortgage_downloader_citadel_prepare_reason"] = mortgage_data.get("mortgage_downloader_citadel_prepare_reason")
        report["mortgage_downloader_citadel_prepare_successful_password_request_found"] = mortgage_data.get("mortgage_downloader_citadel_prepare_successful_password_request_found")
        report["mortgage_downloader_citadel_prepare_bw_session_status"] = mortgage_data.get("mortgage_downloader_citadel_prepare_bw_session_status")
        report["mortgage_downloader_citadel_prepare_bw_item_found"] = mortgage_data.get("mortgage_downloader_citadel_prepare_bw_item_found")
        report["mortgage_downloader_citadel_prepare_bw_item_name"] = mortgage_data.get("mortgage_downloader_citadel_prepare_bw_item_name")
        report["mortgage_downloader_citadel_prepare_bw_item_uri_host_match"] = mortgage_data.get("mortgage_downloader_citadel_prepare_bw_item_uri_host_match")
        report["mortgage_downloader_citadel_prepare_username_matches_har"] = mortgage_data.get("mortgage_downloader_citadel_prepare_username_matches_har")
        report["mortgage_downloader_citadel_prepare_password_matched_before_update"] = mortgage_data.get("mortgage_downloader_citadel_prepare_password_matched_before_update")
        report["mortgage_downloader_citadel_prepare_password_updated"] = mortgage_data.get("mortgage_downloader_citadel_prepare_password_updated")
        report["mortgage_downloader_citadel_prepare_bw_sync_attempted"] = mortgage_data.get("mortgage_downloader_citadel_prepare_bw_sync_attempted")
        report["mortgage_downloader_citadel_auth_retryable_portal_failure"] = mortgage_data.get("mortgage_downloader_citadel_auth_retryable_portal_failure")
        report["mortgage_downloader_citadel_auth_retryable_reason"] = mortgage_data.get("mortgage_downloader_citadel_auth_retryable_reason")
        report["mortgage_downloader_citadel_capture_report"] = mortgage_data.get("mortgage_downloader_citadel_capture_report")
        report["mortgage_downloader_citadel_capture_status"] = mortgage_data.get("mortgage_downloader_citadel_capture_status")
        report["mortgage_downloader_citadel_capture_reason"] = mortgage_data.get("mortgage_downloader_citadel_capture_reason")
        report["mortgage_downloader_citadel_capture_har_token_available"] = mortgage_data.get("mortgage_downloader_citadel_capture_har_token_available")
        report["mortgage_downloader_citadel_capture_har_token_entry_count"] = mortgage_data.get("mortgage_downloader_citadel_capture_har_token_entry_count")
        report["mortgage_downloader_citadel_capture_har_token_mobile_source_id_available"] = mortgage_data.get("mortgage_downloader_citadel_capture_har_token_mobile_source_id_available")
        report["mortgage_downloader_citadel_capture_har_token_source_endpoint_paths"] = mortgage_data.get("mortgage_downloader_citadel_capture_har_token_source_endpoint_paths")
        report["mortgage_downloader_citadel_capture_har_token_source_document_detail_id_count"] = mortgage_data.get("mortgage_downloader_citadel_capture_har_token_source_document_detail_id_count")
        report["mortgage_downloader_citadel_capture_har_token_endpoint_statuses"] = mortgage_data.get("mortgage_downloader_citadel_capture_har_token_endpoint_statuses")
        report["mortgage_downloader_citadel_capture_next_action_status"] = mortgage_data.get("mortgage_downloader_citadel_capture_next_action_status")
        report["mortgage_downloader_citadel_capture_next_action_reason"] = mortgage_data.get("mortgage_downloader_citadel_capture_next_action_reason")
        report["mortgage_downloader_citadel_capture_next_action_command"] = mortgage_data.get("mortgage_downloader_citadel_capture_next_action_command")
        report["mortgage_downloader_citadel_capture_next_action_capture_command"] = mortgage_data.get("mortgage_downloader_citadel_capture_next_action_capture_command")
        report["mortgage_downloader_citadel_capture_next_action_capture_required"] = mortgage_data.get("mortgage_downloader_citadel_capture_next_action_capture_required")
        report["mortgage_downloader_citadel_capture_next_action_target_month"] = mortgage_data.get("mortgage_downloader_citadel_capture_next_action_target_month")
        report["mortgage_downloader_citadel_capture_next_action_target_month_replayable_document_available"] = mortgage_data.get("mortgage_downloader_citadel_capture_next_action_target_month_replayable_document_available")
        report["mortgage_downloader_citadel_capture_next_action_target_month_replayable_document_payload_count"] = mortgage_data.get("mortgage_downloader_citadel_capture_next_action_target_month_replayable_document_payload_count")
        report["mortgage_downloader_citadel_capture_next_action_replayable_statement_months"] = mortgage_data.get("mortgage_downloader_citadel_capture_next_action_replayable_statement_months")
        report["mortgage_downloader_citadel_capture_next_action_statement_document_months"] = mortgage_data.get("mortgage_downloader_citadel_capture_next_action_statement_document_months")
        report["mortgage_downloader_citadel_capture_next_action_required_response_paths"] = mortgage_data.get("mortgage_downloader_citadel_capture_next_action_required_response_paths")
        report["mortgage_downloader_citadel_capture_next_action_required_response_path_counts"] = mortgage_data.get("mortgage_downloader_citadel_capture_next_action_required_response_path_counts")
        report["mortgage_downloader_citadel_capture_next_action_required_response_path_progress"] = mortgage_data.get("mortgage_downloader_citadel_capture_next_action_required_response_path_progress")
        report["mortgage_downloader_citadel_capture_next_action_response_body_requirements"] = mortgage_data.get("mortgage_downloader_citadel_capture_next_action_response_body_requirements")
        report["mortgage_downloader_citadel_capture_manual_auth_required"] = mortgage_data.get("mortgage_downloader_citadel_capture_manual_auth_required")
        report["mortgage_downloader_citadel_capture_manual_auth_file"] = mortgage_data.get("mortgage_downloader_citadel_capture_manual_auth_file")
        report["mortgage_downloader_citadel_capture_manual_auth_portal_url"] = mortgage_data.get("mortgage_downloader_citadel_capture_manual_auth_portal_url")
        report["mortgage_downloader_citadel_capture_manual_auth_target_id"] = mortgage_data.get("mortgage_downloader_citadel_capture_manual_auth_target_id")
        report["mortgage_downloader_citadel_capture_manual_auth_next_command"] = mortgage_data.get("mortgage_downloader_citadel_capture_manual_auth_next_command")
        report["mortgage_downloader_citadel_capture_manual_auth_install_verified_har_dry_run_command"] = mortgage_data.get("mortgage_downloader_citadel_capture_manual_auth_install_verified_har_dry_run_command")
        report["mortgage_downloader_citadel_capture_manual_auth_install_verified_har_apply_command"] = mortgage_data.get("mortgage_downloader_citadel_capture_manual_auth_install_verified_har_apply_command")
        report["mortgage_downloader_citadel_capture_authenticated_found"] = mortgage_data.get("mortgage_downloader_citadel_capture_authenticated_found")
        report["mortgage_downloader_citadel_capture_candidate_count"] = mortgage_data.get("mortgage_downloader_citadel_capture_candidate_count")
        report["mortgage_downloader_citadel_capture_route_counts"] = mortgage_data.get("mortgage_downloader_citadel_capture_route_counts")
        report["mortgage_downloader_citadel_capture_login_tab_count"] = mortgage_data.get("mortgage_downloader_citadel_capture_login_tab_count")
        report["mortgage_downloader_citadel_capture_non_login_tab_count"] = mortgage_data.get("mortgage_downloader_citadel_capture_non_login_tab_count")
        report["mortgage_downloader_citadel_capture_scanned_count"] = mortgage_data.get("mortgage_downloader_citadel_capture_scanned_count")
        report["mortgage_downloader_citadel_capture_captured_endpoint_count"] = mortgage_data.get("mortgage_downloader_citadel_capture_captured_endpoint_count")
        report["mortgage_downloader_citadel_capture_captured_response_body_count"] = mortgage_data.get("mortgage_downloader_citadel_capture_captured_response_body_count")
        report["mortgage_downloader_citadel_capture_source_har_path_exists"] = mortgage_data.get("mortgage_downloader_citadel_capture_source_har_path_exists")
        report["mortgage_downloader_citadel_capture_source_direct_pdf_limit"] = mortgage_data.get("mortgage_downloader_citadel_capture_source_direct_pdf_limit")
        report["mortgage_downloader_citadel_capture_source_direct_pdf_candidate_count"] = mortgage_data.get("mortgage_downloader_citadel_capture_source_direct_pdf_candidate_count")
        report["mortgage_downloader_citadel_capture_source_direct_pdf_path_counts"] = mortgage_data.get("mortgage_downloader_citadel_capture_source_direct_pdf_path_counts")
        report["mortgage_downloader_citadel_capture_source_direct_pdf_fetched_count"] = mortgage_data.get("mortgage_downloader_citadel_capture_source_direct_pdf_fetched_count")
        report["mortgage_downloader_citadel_capture_source_direct_pdf_replayable_count"] = mortgage_data.get("mortgage_downloader_citadel_capture_source_direct_pdf_replayable_count")
        report["mortgage_downloader_citadel_capture_source_required_response_candidate_count"] = mortgage_data.get("mortgage_downloader_citadel_capture_source_required_response_candidate_count")
        report["mortgage_downloader_citadel_capture_source_required_response_path_counts"] = mortgage_data.get("mortgage_downloader_citadel_capture_source_required_response_path_counts")
        report["mortgage_downloader_citadel_capture_source_required_response_fetched_count"] = mortgage_data.get("mortgage_downloader_citadel_capture_source_required_response_fetched_count")
        report["mortgage_downloader_citadel_capture_source_required_response_replayable_count"] = mortgage_data.get("mortgage_downloader_citadel_capture_source_required_response_replayable_count")
        report["mortgage_downloader_citadel_capture_required_response_paths"] = mortgage_data.get("mortgage_downloader_citadel_capture_required_response_paths")
        report["mortgage_downloader_citadel_capture_required_response_path_counts"] = mortgage_data.get("mortgage_downloader_citadel_capture_required_response_path_counts")
        report["mortgage_downloader_citadel_capture_required_response_path_progress"] = mortgage_data.get("mortgage_downloader_citadel_capture_required_response_path_progress")
        report["mortgage_downloader_citadel_capture_response_body_requirements"] = mortgage_data.get("mortgage_downloader_citadel_capture_response_body_requirements")
        report["mortgage_downloader_citadel_capture_response_body_requirement_role_counts"] = mortgage_data.get("mortgage_downloader_citadel_capture_response_body_requirement_role_counts")
        report["mortgage_downloader_citadel_capture_captured_response_body_requirement_counts"] = mortgage_data.get("mortgage_downloader_citadel_capture_captured_response_body_requirement_counts")
        report["mortgage_downloader_citadel_capture_response_body_requirement_role_capture_counts"] = mortgage_data.get("mortgage_downloader_citadel_capture_response_body_requirement_role_capture_counts")
        report["mortgage_downloader_citadel_capture_missing_response_body_requirements"] = mortgage_data.get("mortgage_downloader_citadel_capture_missing_response_body_requirements")
        report["mortgage_downloader_citadel_capture_missing_response_body_requirement_count"] = mortgage_data.get("mortgage_downloader_citadel_capture_missing_response_body_requirement_count")
        report["mortgage_downloader_citadel_capture_captured_required_response_paths"] = mortgage_data.get("mortgage_downloader_citadel_capture_captured_required_response_paths")
        report["mortgage_downloader_citadel_capture_captured_required_response_path_counts"] = mortgage_data.get("mortgage_downloader_citadel_capture_captured_required_response_path_counts")
        report["mortgage_downloader_citadel_capture_missing_required_response_paths"] = mortgage_data.get("mortgage_downloader_citadel_capture_missing_required_response_paths")
        report["mortgage_downloader_citadel_capture_missing_required_response_path_counts"] = mortgage_data.get("mortgage_downloader_citadel_capture_missing_required_response_path_counts")
        report["mortgage_downloader_citadel_capture_statement_candidate_count"] = mortgage_data.get("mortgage_downloader_citadel_capture_statement_candidate_count")
        report["mortgage_downloader_citadel_capture_replayable_document_payload_count"] = mortgage_data.get("mortgage_downloader_citadel_capture_replayable_document_payload_count")
        report["mortgage_downloader_citadel_capture_target_month"] = mortgage_data.get("mortgage_downloader_citadel_capture_target_month")
        report["mortgage_downloader_citadel_capture_target_month_replayable_document_available"] = mortgage_data.get("mortgage_downloader_citadel_capture_target_month_replayable_document_available")
        report["mortgage_downloader_citadel_capture_target_month_replayable_document_payload_count"] = mortgage_data.get("mortgage_downloader_citadel_capture_target_month_replayable_document_payload_count")
        report["mortgage_downloader_citadel_capture_replayable_statement_months"] = mortgage_data.get("mortgage_downloader_citadel_capture_replayable_statement_months")
        report["mortgage_downloader_citadel_capture_statement_document_months"] = mortgage_data.get("mortgage_downloader_citadel_capture_statement_document_months")
        report["mortgage_downloader_citadel_capture_direct_pdf_response_count"] = mortgage_data.get("mortgage_downloader_citadel_capture_direct_pdf_response_count")
        report["mortgage_downloader_citadel_capture_har_path"] = mortgage_data.get("mortgage_downloader_citadel_capture_har_path")
        report["mortgage_downloader_citadel_capture_error_count"] = mortgage_data.get("mortgage_downloader_citadel_capture_error_count")
        report["mortgage_downloader_citadel_report_status"] = mortgage_data.get("mortgage_downloader_citadel_report_status")
        report["mortgage_downloader_citadel_downloaded_count"] = mortgage_data.get("mortgage_downloader_citadel_downloaded_count")
        report["mortgage_downloader_citadel_skipped_count"] = mortgage_data.get("mortgage_downloader_citadel_skipped_count")
        report["mortgage_downloader_citadel_target_month"] = mortgage_data.get("mortgage_downloader_citadel_target_month")
        report["mortgage_downloader_citadel_target_month_statement_available"] = mortgage_data.get("mortgage_downloader_citadel_target_month_statement_available")
        report["mortgage_downloader_citadel_target_month_existing_count"] = mortgage_data.get("mortgage_downloader_citadel_target_month_existing_count")
        report["mortgage_downloader_citadel_target_month_downloaded_count"] = mortgage_data.get("mortgage_downloader_citadel_target_month_downloaded_count")
        report["mortgage_downloader_citadel_target_month_skipped_count"] = mortgage_data.get("mortgage_downloader_citadel_target_month_skipped_count")
        report["mortgage_downloader_citadel_existing_target_month_files"] = mortgage_data.get("mortgage_downloader_citadel_existing_target_month_files")
        report["mortgage_downloader_citadel_downloaded_target_month_files"] = mortgage_data.get("mortgage_downloader_citadel_downloaded_target_month_files")
        report["mortgage_downloader_citadel_skipped_target_month_files"] = mortgage_data.get("mortgage_downloader_citadel_skipped_target_month_files")
        report["mortgage_downloader_citadel_safe_to_run_automatically"] = mortgage_data.get("mortgage_downloader_citadel_safe_to_run_automatically")
        report["mortgage_downloader_citadel_idempotent_replay_safe"] = mortgage_data.get("mortgage_downloader_citadel_idempotent_replay_safe")
        report["mortgage_downloader_citadel_copy_plan_safe_to_apply_automatically"] = mortgage_data.get("mortgage_downloader_citadel_copy_plan_safe_to_apply_automatically")
        report["mortgage_downloader_citadel_har_replay_ready_to_run_automatically"] = mortgage_data.get("mortgage_downloader_citadel_har_replay_ready_to_run_automatically")
        report["mortgage_downloader_citadel_automation_readiness_status"] = mortgage_data.get("mortgage_downloader_citadel_automation_readiness_status")
        report["mortgage_downloader_citadel_automation_blockers"] = mortgage_data.get("mortgage_downloader_citadel_automation_blockers")
        report["mortgage_downloader_citadel_idempotent_skip"] = mortgage_data.get("mortgage_downloader_citadel_idempotent_skip")
        report["mortgage_downloader_citadel_idempotent_skip_reason"] = mortgage_data.get("mortgage_downloader_citadel_idempotent_skip_reason")
        report["mortgage_downloader_citadel_error_count"] = mortgage_data.get("mortgage_downloader_citadel_error_count")
        report["mortgage_downloader_citadel_warning_count"] = mortgage_data.get("mortgage_downloader_citadel_warning_count")
        report["mortgage_downloader_citadel_auth_state"] = mortgage_data.get("mortgage_downloader_citadel_auth_state")
        report["mortgage_downloader_citadel_credentials_available"] = mortgage_data.get("mortgage_downloader_citadel_credentials_available")
        report["mortgage_downloader_citadel_login_mode"] = mortgage_data.get("mortgage_downloader_citadel_login_mode")
        report["mortgage_downloader_citadel_otp_required"] = mortgage_data.get("mortgage_downloader_citadel_otp_required")
        report["mortgage_downloader_citadel_otp_wait_ms"] = mortgage_data.get("mortgage_downloader_citadel_otp_wait_ms")
        report["mortgage_downloader_citadel_otp_file"] = mortgage_data.get("mortgage_downloader_citadel_otp_file")
        report["mortgage_downloader_citadel_otp_required_file"] = mortgage_data.get("mortgage_downloader_citadel_otp_required_file")
        report["mortgage_downloader_citadel_otp_next_command"] = mortgage_data.get("mortgage_downloader_citadel_otp_next_command")
        report["mortgage_downloader_citadel_manual_auth_required"] = mortgage_data.get("mortgage_downloader_citadel_manual_auth_required")
        report["mortgage_downloader_citadel_manual_auth_reason"] = mortgage_data.get("mortgage_downloader_citadel_manual_auth_reason")
        report["mortgage_downloader_citadel_manual_auth_file"] = mortgage_data.get("mortgage_downloader_citadel_manual_auth_file")
        report["mortgage_downloader_citadel_manual_auth_portal_url"] = mortgage_data.get("mortgage_downloader_citadel_manual_auth_portal_url")
        report["mortgage_downloader_citadel_auth_failure_reason"] = mortgage_data.get("mortgage_downloader_citadel_auth_failure_reason")
        report["mortgage_downloader_citadel_auth_failure_visible_reason"] = mortgage_data.get("mortgage_downloader_citadel_auth_failure_visible_reason")
        report["mortgage_downloader_citadel_auth_visible_error"] = mortgage_data.get("mortgage_downloader_citadel_auth_visible_error")
        report["mortgage_downloader_citadel_credential_state_drift_suspected"] = mortgage_data.get("mortgage_downloader_citadel_credential_state_drift_suspected")
        report["mortgage_downloader_citadel_login_form_last_result"] = mortgage_data.get("mortgage_downloader_citadel_login_form_last_result")
        report["mortgage_downloader_citadel_oauth_password_grant_failure_count"] = mortgage_data.get("mortgage_downloader_citadel_oauth_password_grant_failure_count")
        report["mortgage_downloader_citadel_oauth_password_grant_error_codes"] = mortgage_data.get("mortgage_downloader_citadel_oauth_password_grant_error_codes")
        report["mortgage_downloader_citadel_browser_storage_bearer_token_available"] = mortgage_data.get("mortgage_downloader_citadel_browser_storage_bearer_token_available")
        report["mortgage_downloader_citadel_browser_storage_mobile_source_id_available"] = mortgage_data.get("mortgage_downloader_citadel_browser_storage_mobile_source_id_available")
        report["mortgage_downloader_citadel_browser_storage_token_candidate_count"] = mortgage_data.get("mortgage_downloader_citadel_browser_storage_token_candidate_count")
        report["mortgage_downloader_citadel_api_header_mobile_source_id_available"] = mortgage_data.get("mortgage_downloader_citadel_api_header_mobile_source_id_available")
        report["mortgage_downloader_citadel_api_header_authorization_enabled"] = mortgage_data.get("mortgage_downloader_citadel_api_header_authorization_enabled")
        report["mortgage_downloader_citadel_direct_auth_status"] = mortgage_data.get("mortgage_downloader_citadel_direct_auth_status")
        report["mortgage_downloader_citadel_direct_auth_transport"] = mortgage_data.get("mortgage_downloader_citadel_direct_auth_transport")
        report["mortgage_downloader_citadel_direct_browser_fallback_attempted"] = mortgage_data.get("mortgage_downloader_citadel_direct_browser_fallback_attempted")
        report["mortgage_downloader_citadel_direct_browser_fallback_authenticated_found"] = mortgage_data.get("mortgage_downloader_citadel_direct_browser_fallback_authenticated_found")
        report["mortgage_downloader_citadel_direct_fresh_mfa_source_status"] = mortgage_data.get("mortgage_downloader_citadel_direct_fresh_mfa_source_status")
        report["mortgage_downloader_citadel_direct_fresh_recaptcha_token_available"] = mortgage_data.get("mortgage_downloader_citadel_direct_fresh_recaptcha_token_available")
        report["mortgage_downloader_citadel_direct_fresh_recaptcha_token_length"] = mortgage_data.get("mortgage_downloader_citadel_direct_fresh_recaptcha_token_length")
        report["mortgage_downloader_citadel_direct_recaptcha_eval_stage"] = mortgage_data.get("mortgage_downloader_citadel_direct_recaptcha_eval_stage")
        report["mortgage_downloader_citadel_direct_recaptcha_eval_error"] = mortgage_data.get("mortgage_downloader_citadel_direct_recaptcha_eval_error")
        report["mortgage_downloader_citadel_direct_recaptcha_token_action"] = mortgage_data.get("mortgage_downloader_citadel_direct_recaptcha_token_action")
        report["mortgage_downloader_citadel_direct_recaptcha_action_errors"] = mortgage_data.get("mortgage_downloader_citadel_direct_recaptcha_action_errors")
        report["mortgage_downloader_citadel_direct_mfa_process_id_header_available"] = mortgage_data.get("mortgage_downloader_citadel_direct_mfa_process_id_header_available")
        report["mortgage_downloader_citadel_direct_mfa_detail_available"] = mortgage_data.get("mortgage_downloader_citadel_direct_mfa_detail_available")
        report["mortgage_downloader_citadel_direct_mfa_request_uuid_available"] = mortgage_data.get("mortgage_downloader_citadel_direct_mfa_request_uuid_available")
        report["mortgage_downloader_citadel_direct_mfa_request_uuid_source"] = mortgage_data.get("mortgage_downloader_citadel_direct_mfa_request_uuid_source")
        report["mortgage_downloader_citadel_direct_no_mfa_handoff_after_password"] = mortgage_data.get("mortgage_downloader_citadel_direct_no_mfa_handoff_after_password")
        report["mortgage_downloader_citadel_direct_no_mfa_handoff_reason"] = mortgage_data.get("mortgage_downloader_citadel_direct_no_mfa_handoff_reason")
        report["mortgage_downloader_citadel_direct_otp_send_via_type"] = mortgage_data.get("mortgage_downloader_citadel_direct_otp_send_via_type")
        report["mortgage_downloader_citadel_direct_cdp_mfa_process_id_header_available"] = mortgage_data.get("mortgage_downloader_citadel_direct_cdp_mfa_process_id_header_available")
        report["mortgage_downloader_citadel_direct_oauth_cdp_event_count"] = mortgage_data.get("mortgage_downloader_citadel_direct_oauth_cdp_event_count")
        report["mortgage_downloader_citadel_direct_oauth_cdp_last_status"] = mortgage_data.get("mortgage_downloader_citadel_direct_oauth_cdp_last_status")
        report["mortgage_downloader_citadel_direct_password_token_request_shape_matches_har_success"] = mortgage_data.get("mortgage_downloader_citadel_direct_password_token_request_shape_matches_har_success")
        report["mortgage_downloader_citadel_direct_password_token_request_shape_matches_har_failure"] = mortgage_data.get("mortgage_downloader_citadel_direct_password_token_request_shape_matches_har_failure")
        report["mortgage_downloader_citadel_direct_password_token_request_shape_matched_har_statuses"] = mortgage_data.get("mortgage_downloader_citadel_direct_password_token_request_shape_matched_har_statuses")
        report["mortgage_downloader_citadel_direct_password_token_request_structure_matches_har_success"] = mortgage_data.get("mortgage_downloader_citadel_direct_password_token_request_structure_matches_har_success")
        report["mortgage_downloader_citadel_direct_password_token_request_structure_matches_har_failure"] = mortgage_data.get("mortgage_downloader_citadel_direct_password_token_request_structure_matches_har_failure")
        report["mortgage_downloader_citadel_direct_password_token_request_structure_matched_har_statuses"] = mortgage_data.get("mortgage_downloader_citadel_direct_password_token_request_structure_matched_har_statuses")
        report["mortgage_downloader_citadel_direct_error_codes"] = mortgage_data.get("mortgage_downloader_citadel_direct_error_codes")
        report["mortgage_downloader_citadel_direct_error_categories"] = mortgage_data.get("mortgage_downloader_citadel_direct_error_categories")
        report["mortgage_downloader_citadel_credential_item_name"] = mortgage_data.get("mortgage_downloader_citadel_credential_item_name")
        report["mortgage_downloader_citadel_credential_item_uri_hosts"] = mortgage_data.get("mortgage_downloader_citadel_credential_item_uri_hosts")
        report["mortgage_downloader_citadel_credential_item_portal_host_match"] = mortgage_data.get("mortgage_downloader_citadel_credential_item_portal_host_match")
        report["mortgage_downloader_citadel_credential_item_field_names"] = mortgage_data.get("mortgage_downloader_citadel_credential_item_field_names")
        report["mortgage_downloader_citadel_credential_item_notes_len"] = mortgage_data.get("mortgage_downloader_citadel_credential_item_notes_len")
        report["mortgage_downloader_citadel_credential_login_hint_configured"] = mortgage_data.get("mortgage_downloader_citadel_credential_login_hint_configured")
        report["mortgage_downloader_citadel_credential_username_matches_login_hint"] = mortgage_data.get("mortgage_downloader_citadel_credential_username_matches_login_hint")
        report["mortgage_downloader_citadel_credential_item_name_matches_login_hint"] = mortgage_data.get("mortgage_downloader_citadel_credential_item_name_matches_login_hint")
        report["mortgage_downloader_citadel_credential_field_matches_login_hint"] = mortgage_data.get("mortgage_downloader_citadel_credential_field_matches_login_hint")
        report["mortgage_downloader_citadel_credential_username_len"] = mortgage_data.get("mortgage_downloader_citadel_credential_username_len")
        report["mortgage_downloader_citadel_credential_password_len"] = mortgage_data.get("mortgage_downloader_citadel_credential_password_len")
        report["mortgage_downloader_citadel_credential_username_has_at"] = mortgage_data.get("mortgage_downloader_citadel_credential_username_has_at")
        report["mortgage_downloader_citadel_har_token_credential_match_count"] = mortgage_data.get("mortgage_downloader_citadel_har_token_credential_match_count")
        report["mortgage_downloader_citadel_har_successful_password_token_match"] = mortgage_data.get("mortgage_downloader_citadel_har_successful_password_token_match")
        report["mortgage_downloader_citadel_har_mfa_process_id_header_count"] = mortgage_data.get("mortgage_downloader_citadel_har_mfa_process_id_header_count")
        report["mortgage_downloader_citadel_har_token_attempt_statuses"] = mortgage_data.get("mortgage_downloader_citadel_har_token_attempt_statuses")
        report["mortgage_downloader_citadel_har_workflow_embedded_response_body_count"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_embedded_response_body_count")
        report["mortgage_downloader_citadel_har_workflow_replayable_json_response_count"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_replayable_json_response_count")
        report["mortgage_downloader_citadel_har_workflow_replayable_document_payload_count"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_replayable_document_payload_count")
        report["mortgage_downloader_citadel_har_workflow_target_month"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_target_month")
        report["mortgage_downloader_citadel_har_workflow_target_month_replayable_document_available"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_target_month_replayable_document_available")
        report["mortgage_downloader_citadel_har_workflow_target_month_replayable_document_payload_count"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_target_month_replayable_document_payload_count")
        report["mortgage_downloader_citadel_har_workflow_replayable_statement_months"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_replayable_statement_months")
        report["mortgage_downloader_citadel_har_workflow_statement_document_months"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_statement_document_months")
        report["mortgage_downloader_citadel_har_workflow_direct_pdf_response_count"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_direct_pdf_response_count")
        report["mortgage_downloader_citadel_har_workflow_source_direct_pdf_candidate_count"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_source_direct_pdf_candidate_count")
        report["mortgage_downloader_citadel_har_workflow_source_direct_pdf_path_counts"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_source_direct_pdf_path_counts")
        report["mortgage_downloader_citadel_har_workflow_source_direct_pdf_filenames"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_source_direct_pdf_filenames")
        report["mortgage_downloader_citadel_har_workflow_source_direct_pdf_filename_candidates"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_source_direct_pdf_filename_candidates")
        report["mortgage_downloader_citadel_har_workflow_target_month_direct_pdf_filenames"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_target_month_direct_pdf_filenames")
        report["mortgage_downloader_citadel_har_workflow_target_month_direct_pdf_filename_candidates"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_target_month_direct_pdf_filename_candidates")
        report["mortgage_downloader_citadel_har_workflow_target_month_direct_pdf_body_missing_candidate_count"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_target_month_direct_pdf_body_missing_candidate_count")
        report["mortgage_downloader_citadel_har_workflow_source_required_response_candidate_count"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_source_required_response_candidate_count")
        report["mortgage_downloader_citadel_har_workflow_source_required_response_path_counts"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_source_required_response_path_counts")
        report["mortgage_downloader_citadel_har_workflow_direct_pdf_missing_response_count"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_direct_pdf_missing_response_count")
        report["mortgage_downloader_citadel_har_workflow_direct_pdf_missing_response_paths"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_direct_pdf_missing_response_paths")
        report["mortgage_downloader_citadel_har_workflow_capture_quality_status"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_capture_quality_status")
        report["mortgage_downloader_citadel_har_workflow_replay_blocker"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_replay_blocker")
        report["mortgage_downloader_citadel_har_workflow_missing_response_body_count"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_missing_response_body_count")
        report["mortgage_downloader_citadel_har_workflow_missing_response_body_paths"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_missing_response_body_paths")
        report["mortgage_downloader_citadel_har_workflow_missing_response_body_path_counts"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_missing_response_body_path_counts")
        report["mortgage_downloader_citadel_har_workflow_response_body_requirements"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_response_body_requirements")
        report["mortgage_downloader_citadel_har_workflow_embedded_access_token_count"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_embedded_access_token_count")
        report["mortgage_downloader_citadel_har_workflow_can_replay_documents"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_can_replay_documents")
        report["mortgage_downloader_citadel_har_workflow_next_action_status"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_next_action_status")
        report["mortgage_downloader_citadel_har_workflow_next_action_reason"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_next_action_reason")
        report["mortgage_downloader_citadel_har_workflow_next_action_command"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_next_action_command")
        report["mortgage_downloader_citadel_har_workflow_next_action_capture_command"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_next_action_capture_command")
        report["mortgage_downloader_citadel_har_workflow_next_action_capture_required"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_next_action_capture_required")
        report["mortgage_downloader_citadel_har_workflow_next_action_target_month"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_next_action_target_month")
        report["mortgage_downloader_citadel_har_workflow_next_action_target_month_replayable_document_available"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_next_action_target_month_replayable_document_available")
        report["mortgage_downloader_citadel_har_workflow_next_action_target_month_replayable_document_payload_count"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_next_action_target_month_replayable_document_payload_count")
        report["mortgage_downloader_citadel_har_workflow_next_action_replayable_statement_months"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_next_action_replayable_statement_months")
        report["mortgage_downloader_citadel_har_workflow_next_action_statement_document_months"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_next_action_statement_document_months")
        report["mortgage_downloader_citadel_har_workflow_next_action_required_response_paths"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_next_action_required_response_paths")
        report["mortgage_downloader_citadel_har_workflow_next_action_response_body_requirements"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_next_action_response_body_requirements")
        report["mortgage_downloader_citadel_har_workflow_next_action_source_direct_pdf_filenames"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_next_action_source_direct_pdf_filenames")
        report["mortgage_downloader_citadel_har_workflow_next_action_target_month_direct_pdf_filenames"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_next_action_target_month_direct_pdf_filenames")
        report["mortgage_downloader_citadel_har_workflow_next_action_target_month_direct_pdf_body_missing_candidate_count"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_next_action_target_month_direct_pdf_body_missing_candidate_count")
        report["mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_report"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_report")
        report["mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_dry_run_command"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_dry_run_command")
        report["mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_apply_command"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_apply_command")
        report["mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_direct_dry_run_command"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_direct_dry_run_command")
        report["mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_direct_apply_command"] = mortgage_data.get("mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_direct_apply_command")
        report["mortgage_downloader_citadel_install_verified_capture_report"] = mortgage_data.get("mortgage_downloader_citadel_install_verified_capture_report")
        report["mortgage_downloader_citadel_install_verified_capture_apply_command"] = mortgage_data.get("mortgage_downloader_citadel_install_verified_capture_apply_command")
        report["mortgage_downloader_citadel_install_verified_capture_direct_apply_command"] = mortgage_data.get("mortgage_downloader_citadel_install_verified_capture_direct_apply_command")
        report["mortgage_downloader_citadel_tab_scan_candidate_count"] = mortgage_data.get("mortgage_downloader_citadel_tab_scan_candidate_count")
        report["mortgage_downloader_citadel_tab_scan_limit"] = mortgage_data.get("mortgage_downloader_citadel_tab_scan_limit")
        report["mortgage_downloader_citadel_tab_scan_scanned_count"] = mortgage_data.get("mortgage_downloader_citadel_tab_scan_scanned_count")
        report["mortgage_downloader_citadel_tab_scan_skipped_count"] = mortgage_data.get("mortgage_downloader_citadel_tab_scan_skipped_count")
        report["mortgage_downloader_citadel_tab_scan_fetch_timeout_ms"] = mortgage_data.get("mortgage_downloader_citadel_tab_scan_fetch_timeout_ms")
        report["mortgage_downloader_citadel_tab_scan_target_id_requested"] = mortgage_data.get("mortgage_downloader_citadel_tab_scan_target_id_requested")
        report["mortgage_downloader_citadel_tab_scan_target_id_found"] = mortgage_data.get("mortgage_downloader_citadel_tab_scan_target_id_found")
        report["mortgage_downloader_citadel_tab_scan_authenticated_found"] = mortgage_data.get("mortgage_downloader_citadel_tab_scan_authenticated_found")
        report["mortgage_downloader_citadel_tab_scan_direct_fallback_target_selected"] = mortgage_data.get("mortgage_downloader_citadel_tab_scan_direct_fallback_target_selected")
        report["mortgage_downloader_citadel_tab_scan_direct_fallback_target_id_requested"] = mortgage_data.get("mortgage_downloader_citadel_tab_scan_direct_fallback_target_id_requested")
        report["mortgage_downloader_citadel_tab_scan_direct_fallback_target_id_found"] = mortgage_data.get("mortgage_downloader_citadel_tab_scan_direct_fallback_target_id_found")
        report["mortgage_downloader_citadel_oauth_network_event_count"] = mortgage_data.get("mortgage_downloader_citadel_oauth_network_event_count")
        report["mortgage_downloader_citadel_oauth_network_statuses"] = mortgage_data.get("mortgage_downloader_citadel_oauth_network_statuses")
        report["mortgage_downloader_citadel_oauth_network_error_codes"] = mortgage_data.get("mortgage_downloader_citadel_oauth_network_error_codes")
        report["mortgage_downloader_citadel_oauth_network_request_shape_matched_har_statuses"] = mortgage_data.get("mortgage_downloader_citadel_oauth_network_request_shape_matched_har_statuses")
        report["mortgage_downloader_citadel_oauth_network_request_structure_matched_har_statuses"] = mortgage_data.get("mortgage_downloader_citadel_oauth_network_request_structure_matched_har_statuses")
    except Exception as exc:
        report["mortgage_workflow_report_readable"] = False
        report["mortgage_workflow_gate_status"] = "unreadable"
        report["mortgage_workflow_gate_error"] = str(exc)
if weekly_report.exists():
    try:
        weekly_data = json.loads(weekly_report.read_text(encoding="utf-8"))
        report["weekly_unprocessed_status"] = weekly_data.get("status")
        report["weekly_unprocessed_candidate_count"] = weekly_data.get("candidate_count")
        report["weekly_unprocessed_candidate_duplicate_suppressed_count"] = weekly_data.get("candidate_duplicate_suppressed_count")
        report["weekly_unprocessed_duplicate_key_count"] = weekly_data.get("duplicate_key_count")
        report["weekly_unprocessed_candidate_duplicate_key_count"] = weekly_data.get("candidate_duplicate_key_count")
        report["weekly_duplicate_review_pending_count"] = weekly_data.get("duplicate_review_pending_count")
        report["weekly_duplicate_review_blocking_count"] = weekly_data.get("duplicate_review_blocking_count")
        report["weekly_duplicate_review_allowed_count"] = weekly_data.get("duplicate_review_allowed_count")
        report["weekly_candidate_duplicate_pending_count"] = weekly_data.get("candidate_duplicate_pending_count")
        report["weekly_exact_duplicate_extra_row_count"] = weekly_data.get("exact_duplicate_extra_row_count")
        report["weekly_deduped_reporting_ledger"] = weekly_data.get("deduped_reporting_ledger")
        report["weekly_deduped_reporting_ledger_removed_row_count"] = weekly_data.get("deduped_reporting_ledger_removed_row_count")
        report["weekly_duplicate_allowlist_entry_count"] = weekly_data.get("duplicate_allowlist_entry_count")
        report["weekly_duplicate_allowlist_exists"] = weekly_data.get("duplicate_allowlist_exists")
        report["weekly_duplicate_review_packet"] = weekly_data.get("duplicate_review_packet")
        report["weekly_duplicate_allowlist_template"] = weekly_data.get("duplicate_allowlist_template")
        report["weekly_duplicate_review_count"] = weekly_data.get("duplicate_review_count")
        report["weekly_duplicate_review_severity_counts"] = weekly_data.get("duplicate_review_severity_counts")
        report["weekly_duplicate_review_pending_severity_counts"] = weekly_data.get("duplicate_review_pending_severity_counts")
        report["weekly_duplicate_pending_shape_counts"] = weekly_data.get("duplicate_pending_shape_counts")
        report["weekly_duplicate_suggested_action_counts"] = weekly_data.get("duplicate_suggested_action_counts")
        report["weekly_exact_candidate_duplicate_pending_count"] = weekly_data.get("exact_candidate_duplicate_pending_count")
        report["weekly_notes_only_candidate_duplicate_pending_count"] = weekly_data.get("notes_only_candidate_duplicate_pending_count")
        report["weekly_candidate_duplicate_scope_counts"] = weekly_data.get("candidate_duplicate_scope_counts")
        report["weekly_duplicate_review_next_action"] = weekly_data.get("duplicate_review_next_action")
        weekly_unprocessed_idempotent = (weekly_data.get("idempotency") or {}).get("idempotent")
        report["weekly_unprocessed_state_idempotent"] = weekly_unprocessed_idempotent
        report["weekly_unprocessed_idempotent"] = weekly_unprocessed_idempotent
        report["weekly_unprocessed_generated_at"] = weekly_data.get("generated_at")
    except Exception as exc:
        report["weekly_unprocessed_status"] = "unreadable"
        report["weekly_unprocessed_error"] = str(exc)
if weekly_raw_duplicate_report.exists():
    try:
        raw_weekly_data = json.loads(weekly_raw_duplicate_report.read_text(encoding="utf-8"))
        report["weekly_raw_duplicate_status"] = raw_weekly_data.get("status")
        report["weekly_raw_duplicate_key_count"] = raw_weekly_data.get("duplicate_key_count")
        report["weekly_raw_duplicate_review_blocking_count"] = raw_weekly_data.get("duplicate_review_blocking_count")
        report["weekly_raw_candidate_duplicate_pending_count"] = raw_weekly_data.get("candidate_duplicate_pending_count")
        report["weekly_raw_exact_duplicate_extra_row_count"] = raw_weekly_data.get("exact_duplicate_extra_row_count")
        report["weekly_raw_deduped_removed_row_count"] = raw_weekly_data.get("deduped_reporting_ledger_removed_row_count")
        report["weekly_raw_duplicate_report"] = str(weekly_raw_duplicate_report)
    except Exception as exc:
        report["weekly_raw_duplicate_status"] = "unreadable"
        report["weekly_raw_duplicate_error"] = str(exc)
if ecogl_safe_apply_report.exists():
    try:
        safe_apply_data = json.loads(ecogl_safe_apply_report.read_text(encoding="utf-8"))
        report["ecogl_safe_apply_status"] = safe_apply_data.get("status")
        report["ecogl_safe_apply_mode"] = safe_apply_data.get("mode")
        report["ecogl_safe_apply_output_written"] = safe_apply_data.get("output_written")
        report["ecogl_safe_apply_action_count"] = safe_apply_data.get("safe_action_count")
        report["ecogl_safe_apply_already_clean_count"] = safe_apply_data.get("already_clean_safe_pattern_count")
        report["ecogl_safe_apply_actions_digest"] = safe_apply_data.get("actions_digest")
        report["ecogl_safe_apply_output_digest"] = safe_apply_data.get("output_digest")
        report["weekly_clean_reporting_ledger"] = str(weekly_clean_reporting_ledger)
        report["ecogl_safe_apply_report"] = str(ecogl_safe_apply_report)
        report["ecogl_safe_apply_actions"] = str(ecogl_safe_apply_actions)
        report["ecogl_safe_apply_markdown"] = str(ecogl_safe_apply_markdown)
    except Exception as exc:
        report["ecogl_safe_apply_status"] = "unreadable"
        report["ecogl_safe_apply_error"] = str(exc)
if first_day_pm_fee_quarantine_report.exists():
    try:
        quarantine_data = json.loads(first_day_pm_fee_quarantine_report.read_text(encoding="utf-8"))
        report["first_day_pm_fee_quarantine_status"] = quarantine_data.get("status")
        report["first_day_pm_fee_quarantine_count"] = quarantine_data.get("quarantined_row_count")
        report["first_day_pm_fee_quarantine_remaining_count"] = quarantine_data.get("remaining_first_day_pm_fee_count")
        report["first_day_pm_fee_quarantine_reporting_output_clean"] = quarantine_data.get("reporting_output_clean")
        report["first_day_pm_fee_quarantine_raw_source_mutated"] = quarantine_data.get("raw_source_mutated")
        report["first_day_pm_fee_quarantine_month_counts"] = quarantine_data.get("month_counts")
        report["first_day_pm_fee_quarantine_digest"] = quarantine_data.get("quarantine_digest")
        report["first_day_pm_fee_quarantine_output_digest"] = quarantine_data.get("output_digest")
        report["weekly_safe_category_reporting_ledger"] = str(weekly_safe_category_reporting_ledger)
        report["first_day_pm_fee_quarantine_report"] = str(first_day_pm_fee_quarantine_report)
        report["first_day_pm_fee_quarantine_rows"] = str(first_day_pm_fee_quarantine_rows)
        report["first_day_pm_fee_quarantine_markdown"] = str(first_day_pm_fee_quarantine_markdown)
    except Exception as exc:
        report["first_day_pm_fee_quarantine_status"] = "unreadable"
        report["first_day_pm_fee_quarantine_error"] = str(exc)
if no_dao_mortgage_quarantine_report.exists():
    try:
        no_dao_quarantine_data = json.loads(no_dao_mortgage_quarantine_report.read_text(encoding="utf-8"))
        report["no_dao_mortgage_quarantine_status"] = no_dao_quarantine_data.get("status")
        report["no_dao_mortgage_quarantine_count"] = no_dao_quarantine_data.get("quarantined_row_count")
        report["no_dao_mortgage_quarantine_remaining_count"] = no_dao_quarantine_data.get("remaining_no_dao_mortgage_row_count")
        report["no_dao_mortgage_quarantine_reporting_output_clean"] = no_dao_quarantine_data.get("reporting_output_clean")
        report["no_dao_mortgage_quarantine_raw_source_mutated"] = no_dao_quarantine_data.get("raw_source_mutated")
        report["no_dao_mortgage_quarantine_property_counts"] = no_dao_quarantine_data.get("property_counts")
        report["no_dao_mortgage_quarantine_digest"] = no_dao_quarantine_data.get("quarantine_digest")
        report["no_dao_mortgage_quarantine_output_digest"] = no_dao_quarantine_data.get("output_digest")
        report["weekly_no_dao_mortgage_clean_reporting_ledger"] = str(weekly_no_dao_mortgage_clean_reporting_ledger)
        report["no_dao_mortgage_quarantine_report"] = str(no_dao_mortgage_quarantine_report)
        report["no_dao_mortgage_quarantine_rows"] = str(no_dao_mortgage_quarantine_rows)
        report["no_dao_mortgage_quarantine_markdown"] = str(no_dao_mortgage_quarantine_markdown)
    except Exception as exc:
        report["no_dao_mortgage_quarantine_status"] = "unreadable"
        report["no_dao_mortgage_quarantine_error"] = str(exc)
if source_cleanup_queue_report.exists():
    try:
        source_cleanup_queue_data = json.loads(source_cleanup_queue_report.read_text(encoding="utf-8"))
        report["source_cleanup_queue_status"] = source_cleanup_queue_data.get("status")
        report["source_cleanup_queue_action_count"] = source_cleanup_queue_data.get("action_count")
        report["source_cleanup_queue_missing_id_count"] = source_cleanup_queue_data.get("missing_id_count")
        report["source_cleanup_queue_action_counts"] = source_cleanup_queue_data.get("action_counts")
        report["source_cleanup_queue_live_mutation_attempted"] = source_cleanup_queue_data.get("live_mutation_attempted")
        report["source_cleanup_queue_report"] = str(source_cleanup_queue_report)
        report["source_cleanup_queue_rows"] = str(source_cleanup_queue_rows)
        report["source_cleanup_queue_markdown"] = str(source_cleanup_queue_markdown)
    except Exception as exc:
        report["source_cleanup_queue_status"] = "unreadable"
        report["source_cleanup_queue_error"] = str(exc)
if ecogl_accrual_overlay_report.exists():
    try:
        overlay_data = json.loads(ecogl_accrual_overlay_report.read_text(encoding="utf-8"))
        report["ecogl_accrual_overlay_status"] = overlay_data.get("status")
        report["ecogl_accrual_overlay_eligible_action_count"] = overlay_data.get("eligible_action_count")
        report["ecogl_accrual_overlay_appended_action_count"] = overlay_data.get("appended_action_count")
        report["ecogl_accrual_overlay_already_present_action_count"] = overlay_data.get("already_present_action_count")
        report["ecogl_accrual_overlay_actions_digest"] = overlay_data.get("actions_digest")
        report["ecogl_accrual_overlay_output_digest"] = overlay_data.get("output_digest")
        report["weekly_accrual_overlay_ledger"] = str(weekly_accrual_overlay_ledger)
        report["ecogl_accrual_overlay_report"] = str(ecogl_accrual_overlay_report)
        report["ecogl_accrual_overlay_actions"] = str(ecogl_accrual_overlay_actions)
        report["ecogl_accrual_overlay_markdown"] = str(ecogl_accrual_overlay_markdown)
    except Exception as exc:
        report["ecogl_accrual_overlay_status"] = "unreadable"
        report["ecogl_accrual_overlay_error"] = str(exc)
if cf_sync_report.exists():
    try:
        cf_data = json.loads(cf_sync_report.read_text(encoding="utf-8"))
        report["cf_statement_sync_status"] = cf_data.get("status")
        report["cf_statement_sync_reason"] = cf_data.get("reason")
        report["cf_statement_sync_effective_status"] = cf_data.get("effective_status")
        report["cf_statement_sync_effective_ok"] = cf_data.get("effective_ok")
        report["cf_statement_sync_effective_reason"] = cf_data.get("effective_reason")
        report["cf_statement_sync_effective_blockers"] = cf_data.get("effective_blockers")
        report["cf_statement_sync_return_code"] = cf_data.get("return_code")
        report["cf_statement_sync_conflict_count"] = cf_data.get("conflict_count")
        report["cf_statement_sync_conflict_property_count"] = cf_data.get("conflict_property_count")
        report["cf_statement_sync_conflict_review_packet"] = cf_data.get("conflict_review_packet")
        report["cf_statement_sync_conflict_review_conflict_count"] = cf_data.get("conflict_review_conflict_count")
        report["cf_statement_sync_conflict_review_high_count"] = cf_data.get("conflict_review_high_count")
        report["cf_statement_sync_conflict_review_medium_count"] = cf_data.get("conflict_review_medium_count")
        report["cf_statement_sync_conflict_review_action_counts"] = cf_data.get("conflict_review_action_counts")
        report["cf_statement_sync_conflict_resolution_plan"] = cf_data.get("conflict_resolution_plan")
        report["cf_statement_sync_conflict_resolution_approval_template"] = cf_data.get("conflict_resolution_approval_template")
        report["cf_statement_sync_conflict_resolution_status"] = cf_data.get("conflict_resolution_status")
        report["cf_statement_sync_conflict_resolution_mode"] = cf_data.get("conflict_resolution_mode")
        report["cf_statement_sync_conflict_resolution_applicable_count"] = cf_data.get("conflict_resolution_applicable_count")
        report["cf_statement_sync_conflict_resolution_blocked_count"] = cf_data.get("conflict_resolution_blocked_count")
        report["cf_statement_sync_conflict_resolution_approved_applicable_count"] = cf_data.get("conflict_resolution_approved_applicable_count")
        report["cf_statement_sync_conflict_resolution_status_counts"] = cf_data.get("conflict_resolution_status_counts")
        report["cf_statement_sync_conflict_auto_approval_status"] = cf_data.get("conflict_auto_approval_status")
        report["cf_statement_sync_conflict_auto_approval_count"] = cf_data.get("conflict_auto_approval_count")
        report["cf_statement_sync_conflict_auto_approval_digest"] = cf_data.get("conflict_auto_approval_digest")
        report["cf_statement_sync_conflict_auto_apply_status"] = cf_data.get("conflict_auto_apply_status")
        report["cf_statement_sync_conflict_auto_apply_status_counts"] = cf_data.get("conflict_auto_apply_status_counts")
        report["cf_statement_sync_conflict_auto_apply_approved_applicable_count"] = cf_data.get("conflict_auto_apply_approved_applicable_count")
        report["cf_statement_sync_audit_error_count"] = cf_data.get("audit_error_count")
        report["cf_statement_sync_audit_error_class_counts"] = cf_data.get("audit_error_class_counts")
        report["cf_statement_sync_untagged_gl_rows"] = cf_data.get("untagged_gl_rows")
        report["cf_statement_sync_untagged_review_packet"] = cf_data.get("untagged_review_packet")
        report["cf_statement_sync_untagged_review_row_count"] = cf_data.get("untagged_review_row_count")
        report["cf_statement_sync_untagged_review_auto_suggested_count"] = cf_data.get("untagged_review_auto_suggested_count")
        report["cf_statement_sync_untagged_review_required_count"] = cf_data.get("untagged_review_required_count")
        report["cf_statement_sync_untagged_rule_candidates_packet"] = cf_data.get("untagged_rule_candidates_packet")
        report["cf_statement_sync_untagged_rule_candidates_status"] = cf_data.get("untagged_rule_candidates_status")
        report["cf_statement_sync_untagged_rule_candidate_count"] = cf_data.get("untagged_rule_candidate_count")
        report["cf_statement_sync_untagged_rule_high_confidence_count"] = cf_data.get("untagged_rule_high_confidence_count")
        report["cf_statement_sync_untagged_rule_medium_confidence_count"] = cf_data.get("untagged_rule_medium_confidence_count")
        report["cf_statement_sync_untagged_rule_covered_row_count"] = cf_data.get("untagged_rule_covered_row_count")
        report["cf_statement_sync_canonical_cf_property_count"] = cf_data.get("canonical_cf_property_count")
        report["cf_statement_sync_no_mortgage_debt_policy"] = cf_data.get("no_mortgage_debt_policy")
        report["cf_statement_sync_no_mortgage_debt_checked_property_count"] = cf_data.get("no_mortgage_debt_checked_property_count")
        report["cf_statement_sync_no_mortgage_debt_violation_count"] = cf_data.get("no_mortgage_debt_violation_count")
        report["cf_statement_sync_no_mortgage_debt_violation_properties"] = cf_data.get("no_mortgage_debt_violation_properties")
        report["cf_statement_sync_source_cash_balance_policy"] = cf_data.get("source_cash_balance_policy")
        report["cf_statement_sync_source_cash_balance_checked_property_count"] = cf_data.get("source_cash_balance_checked_property_count")
        report["cf_statement_sync_source_cash_balance_update_count"] = cf_data.get("source_cash_balance_update_count")
        report["cf_statement_sync_source_cash_balance_violation_count"] = cf_data.get("source_cash_balance_violation_count")
        report["cf_statement_sync_source_cash_balance_violation_properties"] = cf_data.get("source_cash_balance_violation_properties")
        report["cf_statement_sync_update_count"] = cf_data.get("cf_statement_update_count")
        report["cf_statement_sync_zero_fill_count"] = cf_data.get("cf_statement_zero_fill_count")
        report["cf_statement_sync_overwrite_formula_update_count"] = cf_data.get("cf_statement_overwrite_formula_update_count")
        report["cf_statement_sync_update_property_count"] = cf_data.get("cf_statement_update_property_count")
        report["cf_statement_sync_update_properties"] = cf_data.get("cf_statement_update_properties")
        report["cf_statement_sync_ignored_cf_candidate_count"] = cf_data.get("ignored_cf_candidate_count")
        report["cf_statement_sync_ignored_legacy_cf_candidate_count"] = cf_data.get("ignored_legacy_cf_candidate_count")
        report["cf_statement_sync_ignored_noncanonical_cf_candidate_count"] = cf_data.get("ignored_noncanonical_cf_candidate_count")
        report["cf_statement_sync_missing_canonical_cf_count"] = cf_data.get("missing_canonical_cf_count")
        report["cf_statement_sync_discovery_report"] = cf_data.get("discovery_report")
        report["cf_statement_sync_review_reasons"] = cf_data.get("review_reasons")
        report["cf_statement_sync_audit_report"] = cf_data.get("audit_report")
        report["cf_statement_sync_review_packet"] = cf_data.get("review_packet")
    except Exception as exc:
        report["cf_statement_sync_status"] = "unreadable"
        report["cf_statement_sync_error"] = str(exc)
if cf_no_gl_property_match_report.exists():
    try:
        no_gl_data = json.loads(cf_no_gl_property_match_report.read_text(encoding="utf-8"))
        report["cf_no_gl_property_match_status"] = no_gl_data.get("status")
        report["cf_no_gl_property_match_count"] = no_gl_data.get("no_gl_property_match_count")
        report["cf_no_gl_property_match_active_monthly_scope_count"] = no_gl_data.get("active_monthly_scope_count")
        report["cf_no_gl_property_match_source_gl_property_count"] = no_gl_data.get("source_gl_property_count")
        report["cf_no_gl_property_match_report"] = str(cf_no_gl_property_match_report)
        report["cf_no_gl_property_match_csv"] = str(cf_no_gl_property_match_csv)
    except Exception as exc:
        report["cf_no_gl_property_match_status"] = "unreadable"
        report["cf_no_gl_property_match_error"] = str(exc)
if cf_review_gate.exists():
    try:
        gate_data = json.loads(cf_review_gate.read_text(encoding="utf-8"))
        report["cf_review_gate_status"] = gate_data.get("status")
        report["cf_review_gate_blocker_count"] = gate_data.get("blocker_count")
        report["cf_review_gate_idempotency_key"] = gate_data.get("idempotency_key")
        report["cf_review_gate_action_queue_digest"] = gate_data.get("action_queue_digest")
        report["cf_review_gate_action_queue_count"] = gate_data.get("action_queue_count") or (gate_data.get("summary") or {}).get("action_queue_count")
        report["cf_review_gate"] = str(cf_review_gate)
        report["cf_review_gate_markdown"] = str(cf_review_gate_markdown)
    except Exception as exc:
        report["cf_review_gate_status"] = "unreadable"
        report["cf_review_gate_error"] = str(exc)
if ecogl_autonomy.exists():
    try:
        autonomy_data = json.loads(ecogl_autonomy.read_text(encoding="utf-8"))
        report["ecogl_autonomy_status"] = autonomy_data.get("status")
        report["ecogl_autonomy_downstream_hold"] = autonomy_data.get("downstream_hold")
        report["ecogl_auto_safe_untagged_row_count"] = autonomy_data.get("safe_auto_untagged_row_count")
        report["ecogl_auto_safe_rule_count"] = autonomy_data.get("safe_auto_rule_count")
        report["ecogl_untagged_exception_row_count"] = autonomy_data.get("untagged_exception_row_count")
        report["ecogl_exception_count"] = autonomy_data.get("exception_count")
        report["ecogl_safe_auto_action_digest"] = autonomy_data.get("safe_auto_action_digest")
        report["ecogl_exception_digest"] = autonomy_data.get("exception_digest")
        report["ecogl_autonomy"] = str(ecogl_autonomy)
        report["ecogl_autonomy_markdown"] = str(ecogl_autonomy_markdown)
        report["ecogl_auto_safe_actions"] = str(ecogl_auto_safe_actions)
        report["ecogl_exceptions"] = str(ecogl_exceptions)
    except Exception as exc:
        report["ecogl_autonomy_status"] = "unreadable"
        report["ecogl_autonomy_error"] = str(exc)
if ecogl_source_fix_plan.exists():
    try:
        source_fix_data = json.loads(ecogl_source_fix_plan.read_text(encoding="utf-8"))
        report["ecogl_source_fix_status"] = source_fix_data.get("status")
        report["ecogl_source_fix_action_count"] = source_fix_data.get("action_count")
        report["ecogl_source_fix_action_type_counts"] = source_fix_data.get("action_type_counts")
        report["ecogl_source_fix_automation_status_counts"] = source_fix_data.get("automation_status_counts")
        report["ecogl_source_fix_digest"] = source_fix_data.get("idempotency_digest")
        report["ecogl_source_fix_mutation_mode"] = source_fix_data.get("mutation_mode")
        report["ecogl_source_fix_baselane_source_write_allowed"] = source_fix_data.get("baselane_source_write_allowed")
        report["ecogl_source_fix_plan"] = str(ecogl_source_fix_plan)
        report["ecogl_source_fix_actions"] = str(ecogl_source_fix_actions)
        report["ecogl_source_fix_markdown"] = str(ecogl_source_fix_markdown)
    except Exception as exc:
        report["ecogl_source_fix_status"] = "unreadable"
        report["ecogl_source_fix_error"] = str(exc)
if ecogl_source_fix_evidence.exists():
    try:
        evidence_data = json.loads(ecogl_source_fix_evidence.read_text(encoding="utf-8"))
        report["ecogl_source_fix_evidence_status"] = evidence_data.get("status")
        report["ecogl_source_fix_evidence_row_count"] = evidence_data.get("row_count")
        report["ecogl_source_fix_evidence_downstream_hold"] = evidence_data.get("downstream_hold")
        report["ecogl_source_fix_evidence"] = str(ecogl_source_fix_evidence)
        report["ecogl_source_fix_evidence_markdown"] = str(ecogl_source_fix_evidence_markdown)
    except Exception as exc:
        report["ecogl_source_fix_evidence_status"] = "unreadable"
        report["ecogl_source_fix_evidence_error"] = str(exc)
if ecogl_source_fix_verifier.exists():
    try:
        verifier_data = json.loads(ecogl_source_fix_verifier.read_text(encoding="utf-8"))
        report["ecogl_source_fix_verifier_status"] = verifier_data.get("status")
        report["ecogl_source_fix_verifier_verified_fixed_count"] = verifier_data.get("verified_fixed_count")
        report["ecogl_source_fix_verifier_remaining_count"] = verifier_data.get("remaining_count")
        report["ecogl_source_fix_verifier_status_counts"] = verifier_data.get("status_counts")
        report["ecogl_source_fix_verifier"] = str(ecogl_source_fix_verifier)
        report["ecogl_source_fix_verifier_markdown"] = str(ecogl_source_fix_verifier_markdown)
    except Exception as exc:
        report["ecogl_source_fix_verifier_status"] = "unreadable"
        report["ecogl_source_fix_verifier_error"] = str(exc)
if ecogl_source_fix_corrections.exists():
    try:
        corrections_data = json.loads(ecogl_source_fix_corrections.read_text(encoding="utf-8"))
        report["ecogl_source_fix_corrections_status"] = corrections_data.get("status")
        report["ecogl_source_fix_corrections_row_count"] = corrections_data.get("row_count")
        report["ecogl_source_fix_corrections_remaining_count"] = corrections_data.get("remaining_count")
        report["ecogl_source_fix_corrections"] = str(ecogl_source_fix_corrections)
        report["ecogl_source_fix_corrections_csv"] = str(ecogl_source_fix_corrections_csv)
        report["ecogl_source_fix_corrections_markdown"] = str(ecogl_source_fix_corrections_markdown)
    except Exception as exc:
        report["ecogl_source_fix_corrections_status"] = "unreadable"
        report["ecogl_source_fix_corrections_error"] = str(exc)
if ecogl_source_fix_approval.exists():
    try:
        approval_data = json.loads(ecogl_source_fix_approval.read_text(encoding="utf-8"))
        report["ecogl_source_fix_approval_status"] = approval_data.get("status")
        report["ecogl_source_fix_approval_row_count"] = approval_data.get("row_count")
        report["ecogl_source_fix_approval_approved_count"] = approval_data.get("approved_count")
        report["ecogl_source_fix_approval_pending_count"] = approval_data.get("pending_count")
        report["ecogl_source_fix_approval_invalid_count"] = approval_data.get("invalid_count")
        report["ecogl_source_fix_approval_structural_issue_count"] = approval_data.get("structural_issue_count")
        report["ecogl_source_fix_approval"] = str(ecogl_source_fix_approval)
        report["ecogl_source_fix_approval_markdown"] = str(ecogl_source_fix_approval_markdown)
        report["ecogl_source_fix_approved_corrections_csv"] = str(ecogl_source_fix_approved_corrections_csv)
    except Exception as exc:
        report["ecogl_source_fix_approval_status"] = "unreadable"
        report["ecogl_source_fix_approval_error"] = str(exc)
if ecogl_source_fix_correction_validation.exists():
    try:
        validation_data = json.loads(ecogl_source_fix_correction_validation.read_text(encoding="utf-8"))
        report["ecogl_source_fix_correction_validation_status"] = validation_data.get("status")
        report["ecogl_source_fix_correction_validation_ready_count"] = validation_data.get("ready_count")
        report["ecogl_source_fix_correction_validation_pending_count"] = validation_data.get("pending_count")
        report["ecogl_source_fix_correction_validation_invalid_count"] = validation_data.get("invalid_count")
        report["ecogl_source_fix_correction_validation"] = str(ecogl_source_fix_correction_validation)
        report["ecogl_source_fix_correction_validation_csv"] = str(ecogl_source_fix_correction_validation_csv)
        report["ecogl_source_fix_correction_validation_markdown"] = str(ecogl_source_fix_correction_validation_markdown)
    except Exception as exc:
        report["ecogl_source_fix_correction_validation_status"] = "unreadable"
        report["ecogl_source_fix_correction_validation_error"] = str(exc)
if ecogl_source_fix_apply_plan.exists():
    try:
        apply_plan_data = json.loads(ecogl_source_fix_apply_plan.read_text(encoding="utf-8"))
        report["ecogl_source_fix_apply_plan_status"] = apply_plan_data.get("status")
        report["ecogl_source_fix_apply_plan_row_count"] = apply_plan_data.get("row_count")
        report["ecogl_source_fix_apply_plan_ready_current_source_index_count"] = apply_plan_data.get("ready_current_source_index_count")
        report["ecogl_source_fix_apply_plan_needs_current_source_index_refresh_count"] = apply_plan_data.get("needs_current_source_index_refresh_count")
        report["ecogl_source_fix_apply_plan_blocked_count"] = apply_plan_data.get("blocked_count")
        report["ecogl_source_fix_apply_plan"] = str(ecogl_source_fix_apply_plan)
        report["ecogl_source_fix_apply_plan_csv"] = str(ecogl_source_fix_apply_plan_csv)
        report["ecogl_source_fix_apply_plan_markdown"] = str(ecogl_source_fix_apply_plan_markdown)
    except Exception as exc:
        report["ecogl_source_fix_apply_plan_status"] = "unreadable"
        report["ecogl_source_fix_apply_plan_error"] = str(exc)
if ecogl_source_fix_apply.exists():
    try:
        apply_data = json.loads(ecogl_source_fix_apply.read_text(encoding="utf-8"))
        report["ecogl_source_fix_apply_status"] = apply_data.get("status")
        report["ecogl_source_fix_apply_mode"] = apply_data.get("mode")
        report["ecogl_source_fix_apply_row_count"] = apply_data.get("row_count")
        report["ecogl_source_fix_apply_ready_to_apply_count"] = apply_data.get("ready_to_apply_count")
        report["ecogl_source_fix_apply_already_applied_count"] = apply_data.get("already_applied_count")
        report["ecogl_source_fix_apply_blocked_count"] = apply_data.get("blocked_count")
        report["ecogl_source_fix_apply_applied_count"] = apply_data.get("applied_count")
        report["ecogl_source_fix_apply_failed_count"] = apply_data.get("failed_count")
        report["ecogl_source_fix_apply_allowed"] = apply_data.get("apply_allowed")
        report["ecogl_source_fix_apply"] = str(ecogl_source_fix_apply)
        report["ecogl_source_fix_apply_csv"] = str(ecogl_source_fix_apply_csv)
        report["ecogl_source_fix_apply_markdown"] = str(ecogl_source_fix_apply_markdown)
        report["ecogl_source_fix_apply_payload"] = str(ecogl_source_fix_apply_payload)
    except Exception as exc:
        report["ecogl_source_fix_apply_status"] = "unreadable"
        report["ecogl_source_fix_apply_error"] = str(exc)
if ecogl_source_fix_action_queue.exists():
    try:
        action_queue_data = json.loads(ecogl_source_fix_action_queue.read_text(encoding="utf-8"))
        report["ecogl_source_fix_action_queue_status"] = action_queue_data.get("status")
        report["ecogl_source_fix_action_queue_row_count"] = action_queue_data.get("row_count")
        report["ecogl_source_fix_action_queue_ready_to_apply_count"] = action_queue_data.get("ready_to_apply_count")
        report["ecogl_source_fix_action_queue_decision_required_count"] = action_queue_data.get("decision_required_count")
        report["ecogl_source_fix_action_queue_group_counts"] = action_queue_data.get("group_counts")
        report["ecogl_source_fix_action_queue_next_action"] = action_queue_data.get("primary_next_action")
        report["ecogl_source_fix_action_queue"] = str(ecogl_source_fix_action_queue)
        report["ecogl_source_fix_action_queue_csv"] = str(ecogl_source_fix_action_queue_csv)
        report["ecogl_source_fix_action_queue_markdown"] = str(ecogl_source_fix_action_queue_markdown)
    except Exception as exc:
        report["ecogl_source_fix_action_queue_status"] = "unreadable"
        report["ecogl_source_fix_action_queue_error"] = str(exc)
if native_split_plan.exists():
    try:
        native_split_data = json.loads(native_split_plan.read_text(encoding="utf-8"))
        report["native_split_plan_status"] = native_split_data.get("status")
        report["native_split_plan_row_count"] = native_split_data.get("row_count")
        report["native_split_plan_ready_native_split_count"] = native_split_data.get("ready_native_split_count")
        report["native_split_plan_handled_native_split_count"] = native_split_data.get("handled_native_split_count")
        report["native_split_plan_already_applied_count"] = native_split_data.get("already_applied_count")
        report["native_split_plan_applied_count"] = native_split_data.get("applied_count")
        report["native_split_plan_blocked_count"] = native_split_data.get("blocked_count")
        report["native_split_plan_escrow_native_split_update_count"] = native_split_data.get("escrow_native_split_update_count")
        report["native_split_plan_escrow_native_split_update_ready_count"] = native_split_data.get("escrow_native_split_update_ready_count")
        report["native_split_plan_escrow_native_split_update_handled_count"] = native_split_data.get("escrow_native_split_update_handled_count")
        report["native_split_plan_escrow_native_split_update_blocked_count"] = native_split_data.get("escrow_native_split_update_blocked_count")
        report["native_split_plan_escrow_native_split_update_properties"] = native_split_data.get("escrow_native_split_update_properties")
        report["native_split_plan_escrow_native_split_updates"] = native_split_data.get("escrow_native_split_updates")
        report["native_split_plan_rule_counts"] = native_split_data.get("rule_counts")
        report["native_split_plan_mutation_mode"] = native_split_data.get("mutation_mode")
        report["native_split_plan_digest"] = native_split_data.get("idempotency_digest")
        report["native_split_plan"] = str(native_split_plan)
        report["native_split_plan_csv"] = str(native_split_plan_csv)
        report["native_split_plan_markdown"] = str(native_split_plan_markdown)
    except Exception as exc:
        report["native_split_plan_status"] = "unreadable"
        report["native_split_plan_error"] = str(exc)
if native_split_apply.exists():
    try:
        native_split_apply_data = json.loads(native_split_apply.read_text(encoding="utf-8"))
        report["native_split_apply_status"] = native_split_apply_data.get("status")
        report["native_split_apply_mode"] = native_split_apply_data.get("mutation_mode")
        report["native_split_apply_enabled"] = native_split_apply_data.get("apply_enabled")
        report["native_split_apply_ready_count"] = native_split_apply_data.get("ready_count")
        report["native_split_apply_dry_run_count"] = native_split_apply_data.get("dry_run_count")
        report["native_split_apply_already_applied_count"] = native_split_apply_data.get("already_applied_count")
        report["native_split_apply_applied_count"] = native_split_apply_data.get("applied_count")
        report["native_split_apply_blocked_count"] = native_split_apply_data.get("blocked_count")
        report["native_split_apply_failure_count"] = native_split_apply_data.get("failure_count")
        report["native_split_apply_deferred_pending_count"] = native_split_apply_data.get("deferred_pending_count")
        report["native_split_apply_reason"] = native_split_apply_data.get("reason")
        report["native_split_apply_escrow_native_split_update_count"] = native_split_apply_data.get("escrow_native_split_update_count")
        report["native_split_apply_escrow_native_split_update_ready_count"] = native_split_apply_data.get("escrow_native_split_update_ready_count")
        report["native_split_apply_escrow_native_split_update_blocked_count"] = native_split_apply_data.get("escrow_native_split_update_blocked_count")
        report["native_split_apply_escrow_native_split_update_dry_run_count"] = native_split_apply_data.get("escrow_native_split_update_dry_run_count")
        report["native_split_apply_escrow_native_split_update_applied_count"] = native_split_apply_data.get("escrow_native_split_update_applied_count")
        report["native_split_apply_escrow_native_split_update_already_applied_count"] = native_split_apply_data.get("escrow_native_split_update_already_applied_count")
        report["native_split_apply_escrow_native_split_update_properties"] = native_split_apply_data.get("escrow_native_split_update_properties")
        report["native_split_apply_escrow_native_split_updates"] = native_split_apply_data.get("escrow_native_split_updates")
        auth_preflight = native_split_apply_data.get("auth_preflight") if isinstance(native_split_apply_data.get("auth_preflight"), dict) else {}
        report["native_split_auth_preflight_status"] = auth_preflight.get("status")
        report["native_split_auth_preflight_ok"] = auth_preflight.get("ok")
        report["native_split_auth_preflight_return_code"] = auth_preflight.get("return_code")
        report["native_split_auth_preflight_error"] = auth_preflight.get("auth_error")
        report["native_split_auth_preflight_url"] = auth_preflight.get("auth_url")
        report["native_split_auth_preflight_report"] = auth_preflight.get("report")
        report["native_split_apply"] = str(native_split_apply)
        report["native_split_apply_state"] = str(native_split_apply_state)
    except Exception as exc:
        report["native_split_apply_status"] = "unreadable"
        report["native_split_apply_error"] = str(exc)
if future_cf_values_report.exists():
    try:
        future_cf_values_data = json.loads(future_cf_values_report.read_text(encoding="utf-8"))
        report["future_cf_values_status"] = future_cf_values_data.get("status")
        report["future_cf_values_mode"] = future_cf_values_data.get("mode")
        report["future_cf_values_cf_file_count"] = future_cf_values_data.get("cf_file_count")
        report["future_cf_values_changed_workbook_count"] = future_cf_values_data.get("changed_workbook_count")
        report["future_cf_values_changed_cell_count"] = future_cf_values_data.get("changed_cell_count")
        report["future_cf_values_unreadable_count"] = future_cf_values_data.get("unreadable_count")
        report["future_cf_values_report"] = str(future_cf_values_report)
    except Exception as exc:
        report["future_cf_values_status"] = "unreadable"
        report["future_cf_values_error"] = str(exc)
if future_cf_values_apply_report.exists():
    try:
        future_cf_values_apply_data = json.loads(future_cf_values_apply_report.read_text(encoding="utf-8"))
        report["future_cf_values_apply_status"] = future_cf_values_apply_data.get("status")
        report["future_cf_values_apply_mode"] = future_cf_values_apply_data.get("mode")
        report["future_cf_values_apply_cf_file_count"] = future_cf_values_apply_data.get("cf_file_count")
        report["future_cf_values_apply_changed_workbook_count"] = future_cf_values_apply_data.get("changed_workbook_count")
        report["future_cf_values_apply_changed_cell_count"] = future_cf_values_apply_data.get("changed_cell_count")
        report["future_cf_values_apply_unreadable_count"] = future_cf_values_apply_data.get("unreadable_count")
        report["future_cf_values_apply_report"] = str(future_cf_values_apply_report)
    except Exception as exc:
        report["future_cf_values_apply_status"] = "unreadable"
        report["future_cf_values_apply_error"] = str(exc)
review_safe_idempotency = {
    "status": report["status"],
    "iso_week": report["iso_week"],
    "last_completed_week": report.get("last_completed_week"),
    "state_file": report["state_file"],
    "state_file_marked_complete": report["state_file_marked_complete"],
    "state_file_unmarked": report["state_file_unmarked"],
    "state_file_unmarked_reason": report["state_file_unmarked_reason"],
    "safe_to_skip_next_run": report["status"] == "ok" and report["state_file_marked_complete"],
    "retry_required": report["status"] == "review",
    "retry_safe_without_duplicate_outputs": (
        report["status"] == "review"
        and report["state_file_marked_complete"] is False
        and report.get("weekly_unprocessed_idempotent") is True
    ),
    "weekly_unprocessed_idempotent": report.get("weekly_unprocessed_idempotent"),
    "weekly_unprocessed_state_idempotent": report.get("weekly_unprocessed_state_idempotent"),
    "deterministic_verification_idempotent": report.get("deterministic_verification_idempotent"),
    "ecogl_safe_apply_status": report.get("ecogl_safe_apply_status"),
    "ecogl_safe_apply_action_count": report.get("ecogl_safe_apply_action_count"),
    "ecogl_safe_apply_output_written": report.get("ecogl_safe_apply_output_written"),
    "ecogl_safe_apply_actions_digest": report.get("ecogl_safe_apply_actions_digest"),
    "first_day_pm_fee_quarantine_status": report.get("first_day_pm_fee_quarantine_status"),
    "first_day_pm_fee_quarantine_count": report.get("first_day_pm_fee_quarantine_count"),
    "first_day_pm_fee_quarantine_remaining_count": report.get("first_day_pm_fee_quarantine_remaining_count"),
    "first_day_pm_fee_quarantine_reporting_output_clean": report.get("first_day_pm_fee_quarantine_reporting_output_clean"),
    "first_day_pm_fee_quarantine_digest": report.get("first_day_pm_fee_quarantine_digest"),
    "weekly_safe_category_reporting_ledger": report.get("weekly_safe_category_reporting_ledger"),
    "weekly_clean_reporting_ledger": report.get("weekly_clean_reporting_ledger"),
    "no_dao_mortgage_quarantine_status": report.get("no_dao_mortgage_quarantine_status"),
    "no_dao_mortgage_quarantine_count": report.get("no_dao_mortgage_quarantine_count"),
    "no_dao_mortgage_quarantine_remaining_count": report.get("no_dao_mortgage_quarantine_remaining_count"),
    "no_dao_mortgage_quarantine_reporting_output_clean": report.get("no_dao_mortgage_quarantine_reporting_output_clean"),
    "no_dao_mortgage_quarantine_digest": report.get("no_dao_mortgage_quarantine_digest"),
    "weekly_no_dao_mortgage_clean_reporting_ledger": report.get("weekly_no_dao_mortgage_clean_reporting_ledger"),
    "source_cleanup_queue_status": report.get("source_cleanup_queue_status"),
    "source_cleanup_queue_action_count": report.get("source_cleanup_queue_action_count"),
    "source_cleanup_queue_missing_id_count": report.get("source_cleanup_queue_missing_id_count"),
    "source_cleanup_queue_action_counts": report.get("source_cleanup_queue_action_counts"),
    "ecogl_accrual_overlay_status": report.get("ecogl_accrual_overlay_status"),
    "ecogl_accrual_overlay_appended_action_count": report.get("ecogl_accrual_overlay_appended_action_count"),
    "ecogl_accrual_overlay_actions_digest": report.get("ecogl_accrual_overlay_actions_digest"),
    "weekly_accrual_overlay_ledger": report.get("weekly_accrual_overlay_ledger"),
    "ecogl_autonomy_status": report.get("ecogl_autonomy_status"),
    "ecogl_auto_safe_untagged_row_count": report.get("ecogl_auto_safe_untagged_row_count"),
    "ecogl_untagged_exception_row_count": report.get("ecogl_untagged_exception_row_count"),
    "ecogl_exception_count": report.get("ecogl_exception_count"),
    "ecogl_safe_auto_action_digest": report.get("ecogl_safe_auto_action_digest"),
    "ecogl_exception_digest": report.get("ecogl_exception_digest"),
    "ecogl_source_fix_status": report.get("ecogl_source_fix_status"),
    "ecogl_source_fix_action_count": report.get("ecogl_source_fix_action_count"),
    "ecogl_source_fix_digest": report.get("ecogl_source_fix_digest"),
    "ecogl_source_fix_baselane_source_write_allowed": report.get("ecogl_source_fix_baselane_source_write_allowed"),
    "ecogl_source_fix_approval_status": report.get("ecogl_source_fix_approval_status"),
    "ecogl_source_fix_approval_approved_count": report.get("ecogl_source_fix_approval_approved_count"),
    "ecogl_source_fix_approval_pending_count": report.get("ecogl_source_fix_approval_pending_count"),
    "ecogl_source_fix_approval_invalid_count": report.get("ecogl_source_fix_approval_invalid_count"),
    "ecogl_source_fix_correction_validation_status": report.get("ecogl_source_fix_correction_validation_status"),
    "ecogl_source_fix_correction_validation_ready_count": report.get("ecogl_source_fix_correction_validation_ready_count"),
    "ecogl_source_fix_correction_validation_pending_count": report.get("ecogl_source_fix_correction_validation_pending_count"),
    "ecogl_source_fix_correction_validation_invalid_count": report.get("ecogl_source_fix_correction_validation_invalid_count"),
    "ecogl_source_fix_apply_plan_status": report.get("ecogl_source_fix_apply_plan_status"),
    "ecogl_source_fix_apply_plan_ready_current_source_index_count": report.get("ecogl_source_fix_apply_plan_ready_current_source_index_count"),
    "ecogl_source_fix_apply_plan_needs_current_source_index_refresh_count": report.get("ecogl_source_fix_apply_plan_needs_current_source_index_refresh_count"),
    "ecogl_source_fix_apply_plan_blocked_count": report.get("ecogl_source_fix_apply_plan_blocked_count"),
    "ecogl_source_fix_apply_status": report.get("ecogl_source_fix_apply_status"),
    "ecogl_source_fix_apply_mode": report.get("ecogl_source_fix_apply_mode"),
    "ecogl_source_fix_apply_ready_to_apply_count": report.get("ecogl_source_fix_apply_ready_to_apply_count"),
    "ecogl_source_fix_apply_already_applied_count": report.get("ecogl_source_fix_apply_already_applied_count"),
    "ecogl_source_fix_apply_blocked_count": report.get("ecogl_source_fix_apply_blocked_count"),
    "ecogl_source_fix_apply_applied_count": report.get("ecogl_source_fix_apply_applied_count"),
    "ecogl_source_fix_apply_failed_count": report.get("ecogl_source_fix_apply_failed_count"),
    "native_split_plan_status": report.get("native_split_plan_status"),
    "native_split_plan_ready_native_split_count": report.get("native_split_plan_ready_native_split_count"),
    "native_split_plan_blocked_count": report.get("native_split_plan_blocked_count"),
    "native_split_plan_escrow_native_split_update_count": report.get("native_split_plan_escrow_native_split_update_count"),
    "native_split_plan_escrow_native_split_update_ready_count": report.get("native_split_plan_escrow_native_split_update_ready_count"),
    "native_split_plan_escrow_native_split_update_handled_count": report.get("native_split_plan_escrow_native_split_update_handled_count"),
    "native_split_plan_escrow_native_split_update_blocked_count": report.get("native_split_plan_escrow_native_split_update_blocked_count"),
    "native_split_plan_escrow_native_split_update_properties": report.get("native_split_plan_escrow_native_split_update_properties"),
    "native_split_plan_mutation_mode": report.get("native_split_plan_mutation_mode"),
    "native_split_plan_digest": report.get("native_split_plan_digest"),
    "native_split_apply_status": report.get("native_split_apply_status"),
    "native_split_apply_mode": report.get("native_split_apply_mode"),
    "native_split_apply_enabled": report.get("native_split_apply_enabled"),
    "native_split_apply_applied_count": report.get("native_split_apply_applied_count"),
    "native_split_apply_already_applied_count": report.get("native_split_apply_already_applied_count"),
    "native_split_apply_failure_count": report.get("native_split_apply_failure_count"),
    "native_split_apply_deferred_pending_count": report.get("native_split_apply_deferred_pending_count"),
    "native_split_apply_reason": report.get("native_split_apply_reason"),
    "native_split_apply_escrow_native_split_update_count": report.get("native_split_apply_escrow_native_split_update_count"),
    "native_split_apply_escrow_native_split_update_ready_count": report.get("native_split_apply_escrow_native_split_update_ready_count"),
    "native_split_apply_escrow_native_split_update_blocked_count": report.get("native_split_apply_escrow_native_split_update_blocked_count"),
    "native_split_apply_escrow_native_split_update_dry_run_count": report.get("native_split_apply_escrow_native_split_update_dry_run_count"),
    "native_split_apply_escrow_native_split_update_applied_count": report.get("native_split_apply_escrow_native_split_update_applied_count"),
    "native_split_apply_escrow_native_split_update_already_applied_count": report.get("native_split_apply_escrow_native_split_update_already_applied_count"),
    "native_split_apply_escrow_native_split_update_properties": report.get("native_split_apply_escrow_native_split_update_properties"),
    "native_split_auth_preflight_status": report.get("native_split_auth_preflight_status"),
    "native_split_auth_preflight_ok": report.get("native_split_auth_preflight_ok"),
    "native_split_auth_preflight_return_code": report.get("native_split_auth_preflight_return_code"),
    "native_split_auth_preflight_error": report.get("native_split_auth_preflight_error"),
    "native_split_auth_preflight_url": report.get("native_split_auth_preflight_url"),
    "native_split_auth_preflight_report": report.get("native_split_auth_preflight_report"),
    "future_cf_values_status": report.get("future_cf_values_status"),
    "future_cf_values_changed_cell_count": report.get("future_cf_values_changed_cell_count"),
    "future_cf_values_report": report.get("future_cf_values_report"),
    "future_cf_values_apply_status": report.get("future_cf_values_apply_status"),
    "future_cf_values_apply_changed_cell_count": report.get("future_cf_values_apply_changed_cell_count"),
    "future_cf_values_apply_report": report.get("future_cf_values_apply_report"),
    "cf_review_gate_idempotency_key": report.get("cf_review_gate_idempotency_key"),
    "cf_review_gate_action_queue_digest": report.get("cf_review_gate_action_queue_digest"),
    "cf_review_gate_action_queue_count": report.get("cf_review_gate_action_queue_count"),
    "cf_review_gate_snapshot_current": bool(
        report.get("cf_review_gate_idempotency_key")
        and report.get("cf_review_gate_action_queue_digest")
        and report.get("cf_review_gate_action_queue_count") is not None
    ),
    "mortgage_workflow_status": report.get("mortgage_workflow_status"),
    "mortgage_workflow_rc": report.get("mortgage_workflow_rc"),
    "mortgage_workflow_report": report.get("mortgage_workflow_report"),
    "mortgage_workflow_gate_status": report.get("mortgage_workflow_gate_status"),
    "mortgage_workflow_gate_reason": report.get("mortgage_workflow_gate_reason"),
    "mortgage_workflow_gate_stamp": report.get("mortgage_workflow_gate_stamp"),
    "mortgage_workflow_gate_idempotency_digest": report.get("mortgage_workflow_gate_idempotency_digest"),
    "mortgage_workflow_mortgage_statement_target_month": report.get("mortgage_workflow_mortgage_statement_target_month"),
    "mortgage_workflow_citadel_statement_target_month": report.get("mortgage_workflow_citadel_statement_target_month"),
    "mortgage_workflow_report_only": report.get("mortgage_workflow_report_only"),
    "mortgage_workflow_live_stages_skipped": report.get("mortgage_workflow_live_stages_skipped"),
    "mortgage_workflow_mortgage_downloader_execution_status": report.get("mortgage_workflow_mortgage_downloader_execution_status"),
    "mortgage_workflow_mortgage_downloader_report_is_live_for_this_run": report.get("mortgage_workflow_mortgage_downloader_report_is_live_for_this_run"),
    "mortgage_workflow_tokenomics_workbook_write_is_live_for_this_run": report.get("mortgage_workflow_tokenomics_workbook_write_is_live_for_this_run"),
    "mortgage_workflow_downloader_rc": report.get("mortgage_workflow_downloader_rc"),
    "mortgage_workflow_citadel_download_rc": report.get("mortgage_workflow_citadel_download_rc"),
    "mortgage_workflow_tokenomics_report": report.get("mortgage_workflow_tokenomics_report"),
    "mortgage_workflow_tokenomics_workbook_write_guard_status": report.get("mortgage_workflow_tokenomics_workbook_write_guard_status"),
    "mortgage_workflow_tokenomics_workbook_write_guard_reason": report.get("mortgage_workflow_tokenomics_workbook_write_guard_reason"),
    "mortgage_workflow_tokenomics_infra_review_ignored_for_workbook_write": report.get("mortgage_workflow_tokenomics_infra_review_ignored_for_workbook_write"),
    "mortgage_workflow_tokenomics_workbook_write_ready": report.get("mortgage_workflow_tokenomics_workbook_write_ready"),
    "mortgage_workflow_tokenomics_workbook_write_gate_status": report.get("mortgage_workflow_tokenomics_workbook_write_gate_status"),
    "mortgage_workflow_tokenomics_workbook_write_ready_property_count": report.get("mortgage_workflow_tokenomics_workbook_write_ready_property_count"),
    "mortgage_workflow_tokenomics_workbook_write_ready_properties": report.get("mortgage_workflow_tokenomics_workbook_write_ready_properties"),
    "mortgage_workflow_tokenomics_workbook_ready_property_write_plan_count": report.get("mortgage_workflow_tokenomics_workbook_ready_property_write_plan_count"),
    "mortgage_workflow_tokenomics_workbook_ready_property_write_plan_properties": report.get("mortgage_workflow_tokenomics_workbook_ready_property_write_plan_properties"),
    "mortgage_workflow_tokenomics_workbook_ready_property_write_plan": report.get("mortgage_workflow_tokenomics_workbook_ready_property_write_plan"),
    "mortgage_workflow_tokenomics_workbook_ready_property_planned_cell_write_count": report.get("mortgage_workflow_tokenomics_workbook_ready_property_planned_cell_write_count"),
    "mortgage_workflow_tokenomics_workbook_ready_property_write_blocked_by_full_gate": report.get("mortgage_workflow_tokenomics_workbook_ready_property_write_blocked_by_full_gate"),
    "mortgage_workflow_tokenomics_workbook_write_blocker_count": report.get("mortgage_workflow_tokenomics_workbook_write_blocker_count"),
    "mortgage_workflow_tokenomics_workbook_write_blocker_properties": report.get("mortgage_workflow_tokenomics_workbook_write_blocker_properties"),
    "mortgage_workflow_tokenomics_workbook_write_blocker_reasons": report.get("mortgage_workflow_tokenomics_workbook_write_blocker_reasons"),
    "mortgage_workflow_tokenomics_workbook_write_blockers": report.get("mortgage_workflow_tokenomics_workbook_write_blockers"),
    "mortgage_workflow_downloader_tokenomics_blocking_gap_count": report.get("mortgage_workflow_downloader_tokenomics_blocking_gap_count"),
    "mortgage_workflow_downloader_tokenomics_blocking_gap_properties": report.get("mortgage_workflow_downloader_tokenomics_blocking_gap_properties"),
    "mortgage_workflow_downloader_citadel_tokenomics_blocking": report.get("mortgage_workflow_downloader_citadel_tokenomics_blocking"),
    "mortgage_workflow_tokenomics_status": report.get("mortgage_workflow_tokenomics_status"),
    "mortgage_workflow_tokenomics_effective_status": report.get("mortgage_workflow_tokenomics_effective_status"),
    "mortgage_workflow_tokenomics_current_month": report.get("mortgage_workflow_tokenomics_current_month"),
    "mortgage_workflow_tokenomics_workbook_save_attempted": report.get("mortgage_workflow_tokenomics_workbook_save_attempted"),
    "mortgage_workflow_tokenomics_workbook_partial_ready_property_write_attempted": report.get("mortgage_workflow_tokenomics_workbook_partial_ready_property_write_attempted"),
    "mortgage_workflow_tokenomics_workbook_partial_ready_property_write_applied": report.get("mortgage_workflow_tokenomics_workbook_partial_ready_property_write_applied"),
    "mortgage_workflow_tokenomics_workbook_partial_ready_property_write_property_count": report.get("mortgage_workflow_tokenomics_workbook_partial_ready_property_write_property_count"),
    "mortgage_workflow_tokenomics_workbook_partial_ready_property_write_properties": report.get("mortgage_workflow_tokenomics_workbook_partial_ready_property_write_properties"),
    "mortgage_workflow_tokenomics_workbook_partial_ready_property_cleared_copied_stale_property_count": report.get("mortgage_workflow_tokenomics_workbook_partial_ready_property_cleared_copied_stale_property_count"),
    "mortgage_workflow_tokenomics_workbook_partial_ready_property_cleared_copied_stale_properties": report.get("mortgage_workflow_tokenomics_workbook_partial_ready_property_cleared_copied_stale_properties"),
    "mortgage_workflow_tokenomics_created_sheet": report.get("mortgage_workflow_tokenomics_created_sheet"),
    "mortgage_workflow_tokenomics_property_column_count": report.get("mortgage_workflow_tokenomics_property_column_count"),
    "mortgage_workflow_tokenomics_co_owner_paid_property_count": report.get("mortgage_workflow_tokenomics_co_owner_paid_property_count"),
    "mortgage_workflow_tokenomics_skipped_non_co_owner_paid_property_count": report.get("mortgage_workflow_tokenomics_skipped_non_co_owner_paid_property_count"),
    "mortgage_workflow_tokenomics_statement_found_count": report.get("mortgage_workflow_tokenomics_statement_found_count"),
    "mortgage_workflow_tokenomics_current_month_statement_found_count": report.get("mortgage_workflow_tokenomics_current_month_statement_found_count"),
    "mortgage_workflow_tokenomics_current_month_statement_gap_count": report.get("mortgage_workflow_tokenomics_current_month_statement_gap_count"),
    "mortgage_workflow_tokenomics_current_month_statement_gap_properties": report.get("mortgage_workflow_tokenomics_current_month_statement_gap_properties"),
    "mortgage_workflow_tokenomics_current_month_statement_gaps": report.get("mortgage_workflow_tokenomics_current_month_statement_gaps"),
    "mortgage_workflow_tokenomics_current_month_statement_gap_remediation_count": report.get("mortgage_workflow_tokenomics_current_month_statement_gap_remediation_count"),
    "mortgage_workflow_tokenomics_current_month_statement_gap_remediation_available_count": report.get("mortgage_workflow_tokenomics_current_month_statement_gap_remediation_available_count"),
    "mortgage_workflow_tokenomics_current_month_statement_gap_remediation_missing_action_count": report.get("mortgage_workflow_tokenomics_current_month_statement_gap_remediation_missing_action_count"),
    "mortgage_workflow_tokenomics_current_month_statement_gap_remediation_missing_action_properties": report.get("mortgage_workflow_tokenomics_current_month_statement_gap_remediation_missing_action_properties"),
    "mortgage_workflow_tokenomics_current_month_statement_gap_remediation_plan": report.get("mortgage_workflow_tokenomics_current_month_statement_gap_remediation_plan"),
    "mortgage_workflow_tokenomics_mortgage_downloader_coverage_report": report.get("mortgage_workflow_tokenomics_mortgage_downloader_coverage_report"),
    "mortgage_workflow_tokenomics_mortgage_downloader_coverage_status": report.get("mortgage_workflow_tokenomics_mortgage_downloader_coverage_status"),
    "mortgage_workflow_tokenomics_mortgage_downloader_coverage_report_readable": report.get("mortgage_workflow_tokenomics_mortgage_downloader_coverage_report_readable"),
    "mortgage_workflow_downloader_credential_refresh_report": report.get("mortgage_workflow_downloader_credential_refresh_report"),
    "mortgage_workflow_downloader_credential_refresh_rc": report.get("mortgage_workflow_downloader_credential_refresh_rc"),
    "mortgage_workflow_downloader_credential_refresh_status": report.get("mortgage_workflow_downloader_credential_refresh_status"),
    "mortgage_workflow_downloader_credential_refresh_profile": report.get("mortgage_workflow_downloader_credential_refresh_profile"),
    "mortgage_workflow_downloader_credential_refresh_ids": report.get("mortgage_workflow_downloader_credential_refresh_ids"),
    "mortgage_workflow_downloader_credential_refresh_timeout_seconds": report.get("mortgage_workflow_downloader_credential_refresh_timeout_seconds"),
    "mortgage_workflow_tokenomics_stale_statement_count": report.get("mortgage_workflow_tokenomics_stale_statement_count"),
    "mortgage_workflow_tokenomics_stale_statement_properties": report.get("mortgage_workflow_tokenomics_stale_statement_properties"),
    "mortgage_workflow_tokenomics_missing_current_month_statement_count": report.get("mortgage_workflow_tokenomics_missing_current_month_statement_count"),
    "mortgage_workflow_tokenomics_missing_current_month_statement_properties": report.get("mortgage_workflow_tokenomics_missing_current_month_statement_properties"),
    "mortgage_workflow_tokenomics_pdf_statement_count": report.get("mortgage_workflow_tokenomics_pdf_statement_count"),
    "mortgage_workflow_tokenomics_statements_found": report.get("mortgage_workflow_tokenomics_statements_found"),
    "mortgage_workflow_tokenomics_balances_written": report.get("mortgage_workflow_tokenomics_balances_written"),
    "mortgage_workflow_tokenomics_statement_reference_write_count": report.get("mortgage_workflow_tokenomics_statement_reference_write_count"),
    "mortgage_workflow_tokenomics_principal_balance_write_count": report.get("mortgage_workflow_tokenomics_principal_balance_write_count"),
    "mortgage_workflow_tokenomics_cell_write_count": report.get("mortgage_workflow_tokenomics_cell_write_count"),
    "mortgage_workflow_tokenomics_safe_to_run_automatically": report.get("mortgage_workflow_tokenomics_safe_to_run_automatically"),
    "mortgage_workflow_tokenomics_statement_record_count": report.get("mortgage_workflow_tokenomics_statement_record_count"),
    "mortgage_workflow_tokenomics_co_owner_paid_statement_record_count": report.get("mortgage_workflow_tokenomics_co_owner_paid_statement_record_count"),
    "mortgage_workflow_tokenomics_co_owner_paid_statement_records": report.get("mortgage_workflow_tokenomics_co_owner_paid_statement_records"),
    "mortgage_workflow_tokenomics_missing_co_owner_paid_statement_properties": report.get("mortgage_workflow_tokenomics_missing_co_owner_paid_statement_properties"),
    "mortgage_workflow_downloader_coverage_rc": report.get("mortgage_workflow_downloader_coverage_rc"),
    "mortgage_workflow_downloader_coverage_report": report.get("mortgage_workflow_downloader_coverage_report"),
    "mortgage_workflow_downloader_coverage_status": report.get("mortgage_workflow_downloader_coverage_status"),
    "mortgage_workflow_downloader_coverage_tokenomics_source": report.get("mortgage_workflow_downloader_coverage_tokenomics_source"),
    "mortgage_workflow_downloader_coverage_tokenomics_fallback_report": report.get("mortgage_workflow_downloader_coverage_tokenomics_fallback_report"),
    "mortgage_workflow_downloader_coverage_tokenomics_fallback_report_used": report.get("mortgage_workflow_downloader_coverage_tokenomics_fallback_report_used"),
    "mortgage_workflow_downloader_coverage_tokenomics_fallback_error": report.get("mortgage_workflow_downloader_coverage_tokenomics_fallback_error"),
    "mortgage_workflow_downloader_coverage_config_readable": report.get("mortgage_workflow_downloader_coverage_config_readable"),
    "mortgage_workflow_downloader_coverage_config_error": report.get("mortgage_workflow_downloader_coverage_config_error"),
    "mortgage_workflow_downloader_coverage_co_owner_paid_property_count": report.get("mortgage_workflow_downloader_coverage_co_owner_paid_property_count"),
    "mortgage_workflow_downloader_coverage_covered_co_owner_paid_property_count": report.get("mortgage_workflow_downloader_coverage_covered_co_owner_paid_property_count"),
    "mortgage_workflow_downloader_coverage_automation_ready_co_owner_paid_property_count": report.get("mortgage_workflow_downloader_coverage_automation_ready_co_owner_paid_property_count"),
    "mortgage_workflow_downloader_coverage_configured_but_not_ready_co_owner_paid_property_count": report.get("mortgage_workflow_downloader_coverage_configured_but_not_ready_co_owner_paid_property_count"),
    "mortgage_workflow_downloader_coverage_configured_downloader_count": report.get("mortgage_workflow_downloader_coverage_configured_downloader_count"),
    "mortgage_workflow_downloader_coverage_configured_downloader_ids": report.get("mortgage_workflow_downloader_coverage_configured_downloader_ids"),
    "mortgage_workflow_downloader_coverage_configured_downloader_properties": report.get("mortgage_workflow_downloader_coverage_configured_downloader_properties"),
    "mortgage_workflow_downloader_coverage_configured_downloader_latest_report_statuses": report.get("mortgage_workflow_downloader_coverage_configured_downloader_latest_report_statuses"),
    "mortgage_workflow_downloader_coverage_configured_downloader_latest_report_reasons": report.get("mortgage_workflow_downloader_coverage_configured_downloader_latest_report_reasons"),
    "mortgage_workflow_downloader_coverage_configured_downloader_blocker_reasons": report.get("mortgage_workflow_downloader_coverage_configured_downloader_blocker_reasons"),
    "mortgage_workflow_downloader_coverage_active_configured_downloader_count": report.get("mortgage_workflow_downloader_coverage_active_configured_downloader_count"),
    "mortgage_workflow_downloader_coverage_active_configured_downloader_ids": report.get("mortgage_workflow_downloader_coverage_active_configured_downloader_ids"),
    "mortgage_workflow_downloader_coverage_automation_ready_configured_downloader_count": report.get("mortgage_workflow_downloader_coverage_automation_ready_configured_downloader_count"),
    "mortgage_workflow_downloader_coverage_automation_ready_configured_downloader_ids": report.get("mortgage_workflow_downloader_coverage_automation_ready_configured_downloader_ids"),
    "mortgage_workflow_downloader_coverage_automation_blocked_configured_downloader_count": report.get("mortgage_workflow_downloader_coverage_automation_blocked_configured_downloader_count"),
    "mortgage_workflow_downloader_coverage_automation_blocked_configured_downloader_ids": report.get("mortgage_workflow_downloader_coverage_automation_blocked_configured_downloader_ids"),
    "mortgage_workflow_downloader_coverage_blocked_configured_downloader_count": report.get("mortgage_workflow_downloader_coverage_blocked_configured_downloader_count"),
    "mortgage_workflow_downloader_coverage_blocked_configured_downloaders": report.get("mortgage_workflow_downloader_coverage_blocked_configured_downloaders"),
    "mortgage_workflow_downloader_coverage_automation_repair_not_statement_gap_count": report.get("mortgage_workflow_downloader_coverage_automation_repair_not_statement_gap_count"),
    "mortgage_workflow_downloader_coverage_automation_repair_not_statement_gap_properties": report.get("mortgage_workflow_downloader_coverage_automation_repair_not_statement_gap_properties"),
    "mortgage_workflow_downloader_coverage_automation_repair_not_statement_gap_downloaders": report.get("mortgage_workflow_downloader_coverage_automation_repair_not_statement_gap_downloaders"),
    "mortgage_workflow_downloader_coverage_current_month_statement_available_count": report.get("mortgage_workflow_downloader_coverage_current_month_statement_available_count"),
    "mortgage_workflow_downloader_coverage_current_month_statement_gap_count": report.get("mortgage_workflow_downloader_coverage_current_month_statement_gap_count"),
    "mortgage_workflow_downloader_coverage_current_month_statement_gap_properties": report.get("mortgage_workflow_downloader_coverage_current_month_statement_gap_properties"),
    "mortgage_workflow_downloader_coverage_current_month_statement_gaps": report.get("mortgage_workflow_downloader_coverage_current_month_statement_gaps"),
    "mortgage_workflow_downloader_coverage_downloader_target_month_statement_gap_count": report.get("mortgage_workflow_downloader_coverage_downloader_target_month_statement_gap_count"),
    "mortgage_workflow_downloader_coverage_downloader_target_month_statement_gap_properties": report.get("mortgage_workflow_downloader_coverage_downloader_target_month_statement_gap_properties"),
    "mortgage_workflow_downloader_coverage_downloader_target_month_statement_gaps": report.get("mortgage_workflow_downloader_coverage_downloader_target_month_statement_gaps"),
    "mortgage_workflow_downloader_coverage_stale_statement_count": report.get("mortgage_workflow_downloader_coverage_stale_statement_count"),
    "mortgage_workflow_downloader_coverage_stale_statement_properties": report.get("mortgage_workflow_downloader_coverage_stale_statement_properties"),
    "mortgage_workflow_downloader_coverage_missing_current_month_statement_count": report.get("mortgage_workflow_downloader_coverage_missing_current_month_statement_count"),
    "mortgage_workflow_downloader_coverage_missing_current_month_statement_properties": report.get("mortgage_workflow_downloader_coverage_missing_current_month_statement_properties"),
    "mortgage_workflow_downloader_coverage_missing_downloader_count": report.get("mortgage_workflow_downloader_coverage_missing_downloader_count"),
    "mortgage_workflow_downloader_coverage_missing_downloader_properties": report.get("mortgage_workflow_downloader_coverage_missing_downloader_properties"),
    "mortgage_workflow_downloader_coverage_workflow_evidence_capture_plan_count": report.get("mortgage_workflow_downloader_coverage_workflow_evidence_capture_plan_count"),
    "mortgage_workflow_downloader_coverage_workflow_evidence_capture_plan": report.get("mortgage_workflow_downloader_coverage_workflow_evidence_capture_plan"),
    "mortgage_workflow_downloader_coverage_next_action_plan_count": report.get("mortgage_workflow_downloader_coverage_next_action_plan_count"),
    "mortgage_workflow_downloader_coverage_next_action_manual_count": report.get("mortgage_workflow_downloader_coverage_next_action_manual_count"),
    "mortgage_workflow_downloader_coverage_next_action_safe_to_run_automatically_count": report.get("mortgage_workflow_downloader_coverage_next_action_safe_to_run_automatically_count"),
    "mortgage_workflow_downloader_coverage_next_action_plan": report.get("mortgage_workflow_downloader_coverage_next_action_plan"),
    "mortgage_workflow_downloader_coverage_safe_to_run_automatically": report.get("mortgage_workflow_downloader_coverage_safe_to_run_automatically"),
    "mortgage_workflow_evidence_advance_rc": report.get("mortgage_workflow_evidence_advance_rc"),
    "mortgage_workflow_evidence_advance_report": report.get("mortgage_workflow_evidence_advance_report"),
    "mortgage_workflow_evidence_advance_status": report.get("mortgage_workflow_evidence_advance_status"),
    "mortgage_workflow_evidence_advance_reason": report.get("mortgage_workflow_evidence_advance_reason"),
    "mortgage_workflow_evidence_advance_property_count": report.get("mortgage_workflow_evidence_advance_property_count"),
    "mortgage_workflow_evidence_advance_ok_count": report.get("mortgage_workflow_evidence_advance_ok_count"),
    "mortgage_workflow_evidence_advance_review_count": report.get("mortgage_workflow_evidence_advance_review_count"),
    "mortgage_workflow_evidence_advance_safe_to_run_automatically": report.get("mortgage_workflow_evidence_advance_safe_to_run_automatically"),
    "mortgage_workflow_evidence_advance_enable_verified_registry_entry": report.get("mortgage_workflow_evidence_advance_enable_verified_registry_entry"),
    "mortgage_workflow_evidence_advance_results": report.get("mortgage_workflow_evidence_advance_results"),
    "mortgage_workflow_evidence_refresh_rc": report.get("mortgage_workflow_evidence_refresh_rc"),
    "mortgage_workflow_evidence_refresh_report": report.get("mortgage_workflow_evidence_refresh_report"),
    "mortgage_workflow_evidence_refresh_idempotency_digest": report.get("mortgage_workflow_evidence_refresh_idempotency_digest"),
    "mortgage_workflow_evidence_refresh_status": report.get("mortgage_workflow_evidence_refresh_status"),
    "mortgage_workflow_evidence_refresh_reason": report.get("mortgage_workflow_evidence_refresh_reason"),
    "mortgage_workflow_evidence_refresh_property_count": report.get("mortgage_workflow_evidence_refresh_property_count"),
    "mortgage_workflow_evidence_refresh_analysis_ready_count": report.get("mortgage_workflow_evidence_refresh_analysis_ready_count"),
    "mortgage_workflow_evidence_refresh_analysis_blocked_count": report.get("mortgage_workflow_evidence_refresh_analysis_blocked_count"),
    "mortgage_workflow_evidence_refresh_scaffold_ready_count": report.get("mortgage_workflow_evidence_refresh_scaffold_ready_count"),
    "mortgage_workflow_evidence_refresh_ensure_handoffs": report.get("mortgage_workflow_evidence_refresh_ensure_handoffs"),
    "mortgage_workflow_evidence_refresh_handoff_pending_property_count": report.get("mortgage_workflow_evidence_refresh_handoff_pending_property_count"),
    "mortgage_workflow_evidence_refresh_handoff_pending_properties": report.get("mortgage_workflow_evidence_refresh_handoff_pending_properties"),
    "mortgage_workflow_evidence_refresh_handoff_file_gap_count": report.get("mortgage_workflow_evidence_refresh_handoff_file_gap_count"),
    "mortgage_workflow_evidence_refresh_handoff_file_gap_properties": report.get("mortgage_workflow_evidence_refresh_handoff_file_gap_properties"),
    "mortgage_workflow_evidence_refresh_har_path_missing_count": report.get("mortgage_workflow_evidence_refresh_har_path_missing_count"),
    "mortgage_workflow_evidence_refresh_har_path_missing_properties": report.get("mortgage_workflow_evidence_refresh_har_path_missing_properties"),
    "mortgage_workflow_evidence_refresh_har_path_present_blocked_count": report.get("mortgage_workflow_evidence_refresh_har_path_present_blocked_count"),
    "mortgage_workflow_evidence_refresh_har_path_present_blocked_properties": report.get("mortgage_workflow_evidence_refresh_har_path_present_blocked_properties"),
    "mortgage_workflow_evidence_refresh_post_capture_check_ready_count": report.get("mortgage_workflow_evidence_refresh_post_capture_check_ready_count"),
    "mortgage_workflow_evidence_refresh_post_capture_check_ready_properties": report.get("mortgage_workflow_evidence_refresh_post_capture_check_ready_properties"),
    "mortgage_workflow_evidence_refresh_capture_required_before_offline_next_step_count": report.get("mortgage_workflow_evidence_refresh_capture_required_before_offline_next_step_count"),
    "mortgage_workflow_evidence_refresh_capture_required_before_offline_next_step_properties": report.get("mortgage_workflow_evidence_refresh_capture_required_before_offline_next_step_properties"),
    "mortgage_workflow_evidence_refresh_offline_next_step_ready_count": report.get("mortgage_workflow_evidence_refresh_offline_next_step_ready_count"),
    "mortgage_workflow_evidence_refresh_offline_next_step_gap_count": report.get("mortgage_workflow_evidence_refresh_offline_next_step_gap_count"),
    "mortgage_workflow_evidence_refresh_offline_next_step_gap_properties": report.get("mortgage_workflow_evidence_refresh_offline_next_step_gap_properties"),
    "mortgage_workflow_evidence_refresh_raw_discovered_har_candidate_property_count": report.get("mortgage_workflow_evidence_refresh_raw_discovered_har_candidate_property_count"),
    "mortgage_workflow_evidence_refresh_raw_discovered_har_candidate_properties": report.get("mortgage_workflow_evidence_refresh_raw_discovered_har_candidate_properties"),
    "mortgage_workflow_evidence_refresh_discovered_har_candidate_property_count": report.get("mortgage_workflow_evidence_refresh_discovered_har_candidate_property_count"),
    "mortgage_workflow_evidence_refresh_discovered_har_candidate_properties": report.get("mortgage_workflow_evidence_refresh_discovered_har_candidate_properties"),
    "mortgage_workflow_evidence_refresh_safe_discovered_har_candidate_property_count": report.get("mortgage_workflow_evidence_refresh_safe_discovered_har_candidate_property_count"),
    "mortgage_workflow_evidence_refresh_safe_discovered_har_candidate_properties": report.get("mortgage_workflow_evidence_refresh_safe_discovered_har_candidate_properties"),
    "mortgage_workflow_evidence_refresh_selected_discovered_har_candidate_count": report.get("mortgage_workflow_evidence_refresh_selected_discovered_har_candidate_count"),
    "mortgage_workflow_evidence_refresh_selected_discovered_har_candidate_properties": report.get("mortgage_workflow_evidence_refresh_selected_discovered_har_candidate_properties"),
    "mortgage_workflow_evidence_refresh_rejected_discovered_har_candidate_count": report.get("mortgage_workflow_evidence_refresh_rejected_discovered_har_candidate_count"),
    "mortgage_workflow_evidence_refresh_rejected_discovered_har_candidate_paths": report.get("mortgage_workflow_evidence_refresh_rejected_discovered_har_candidate_paths"),
    "mortgage_workflow_evidence_refresh_next_action_plan_count": report.get("mortgage_workflow_evidence_refresh_next_action_plan_count"),
    "mortgage_workflow_evidence_refresh_next_action_manual_count": report.get("mortgage_workflow_evidence_refresh_next_action_manual_count"),
    "mortgage_workflow_evidence_refresh_next_action_safe_to_run_automatically_count": report.get("mortgage_workflow_evidence_refresh_next_action_safe_to_run_automatically_count"),
    "mortgage_workflow_evidence_refresh_safe_to_run_automatically": report.get("mortgage_workflow_evidence_refresh_safe_to_run_automatically"),
    "mortgage_workflow_evidence_refresh_next_action_plan": report.get("mortgage_workflow_evidence_refresh_next_action_plan"),
    "mortgage_workflow_replayability_rc": report.get("mortgage_workflow_replayability_rc"),
    "mortgage_workflow_replayability_report": report.get("mortgage_workflow_replayability_report"),
    "mortgage_workflow_replayability_status": report.get("mortgage_workflow_replayability_status"),
    "mortgage_workflow_replayability_reason": report.get("mortgage_workflow_replayability_reason"),
    "mortgage_workflow_replayability_safe_to_run_automatically": report.get("mortgage_workflow_replayability_safe_to_run_automatically"),
    "mortgage_workflow_replayability_har_analysis_count": report.get("mortgage_workflow_replayability_har_analysis_count"),
    "mortgage_workflow_replayability_har_analysis_ready_count": report.get("mortgage_workflow_replayability_har_analysis_ready_count"),
    "mortgage_workflow_replayability_har_analysis_blocked_count": report.get("mortgage_workflow_replayability_har_analysis_blocked_count"),
    "mortgage_workflow_replayability_raw_discovered_har_candidate_property_count": report.get("mortgage_workflow_replayability_raw_discovered_har_candidate_property_count"),
    "mortgage_workflow_replayability_raw_discovered_har_candidate_properties": report.get("mortgage_workflow_replayability_raw_discovered_har_candidate_properties"),
    "mortgage_workflow_replayability_discovered_har_candidate_property_count": report.get("mortgage_workflow_replayability_discovered_har_candidate_property_count"),
    "mortgage_workflow_replayability_discovered_har_candidate_properties": report.get("mortgage_workflow_replayability_discovered_har_candidate_properties"),
    "mortgage_workflow_replayability_safe_discovered_har_candidate_property_count": report.get("mortgage_workflow_replayability_safe_discovered_har_candidate_property_count"),
    "mortgage_workflow_replayability_safe_discovered_har_candidate_properties": report.get("mortgage_workflow_replayability_safe_discovered_har_candidate_properties"),
    "mortgage_workflow_replayability_selected_discovered_har_candidate_count": report.get("mortgage_workflow_replayability_selected_discovered_har_candidate_count"),
    "mortgage_workflow_replayability_selected_discovered_har_candidate_properties": report.get("mortgage_workflow_replayability_selected_discovered_har_candidate_properties"),
    "mortgage_workflow_replayability_rejected_discovered_har_candidate_count": report.get("mortgage_workflow_replayability_rejected_discovered_har_candidate_count"),
    "mortgage_workflow_replayability_rejected_discovered_har_candidate_paths": report.get("mortgage_workflow_replayability_rejected_discovered_har_candidate_paths"),
    "mortgage_workflow_replayability_next_action_plan_count": report.get("mortgage_workflow_replayability_next_action_plan_count"),
    "mortgage_workflow_replayability_next_action_manual_count": report.get("mortgage_workflow_replayability_next_action_manual_count"),
    "mortgage_workflow_replayability_next_action_safe_to_run_automatically_count": report.get("mortgage_workflow_replayability_next_action_safe_to_run_automatically_count"),
    "mortgage_workflow_replayability_next_action_plan": report.get("mortgage_workflow_replayability_next_action_plan"),
    "mortgage_workflow_replayability_citadel_status": report.get("mortgage_workflow_replayability_citadel_status"),
    "mortgage_workflow_replayability_citadel_ready": report.get("mortgage_workflow_replayability_citadel_ready"),
    "mortgage_workflow_replayability_citadel_blocker": report.get("mortgage_workflow_replayability_citadel_blocker"),
    "mortgage_workflow_replayability_citadel_target_month": report.get("mortgage_workflow_replayability_citadel_target_month"),
    "mortgage_workflow_replayability_citadel_target_month_replayable_document_available": report.get("mortgage_workflow_replayability_citadel_target_month_replayable_document_available"),
    "mortgage_workflow_replayability_citadel_target_month_replayable_document_payload_count": report.get("mortgage_workflow_replayability_citadel_target_month_replayable_document_payload_count"),
    "mortgage_workflow_replayability_citadel_replayable_statement_months": report.get("mortgage_workflow_replayability_citadel_replayable_statement_months"),
    "mortgage_workflow_replayability_citadel_statement_document_months": report.get("mortgage_workflow_replayability_citadel_statement_document_months"),
    "mortgage_workflow_replayability_citadel_direct_pdf_response_count": report.get("mortgage_workflow_replayability_citadel_direct_pdf_response_count"),
    "mortgage_workflow_replayability_citadel_direct_pdf_missing_response_count": report.get("mortgage_workflow_replayability_citadel_direct_pdf_missing_response_count"),
    "mortgage_workflow_replayability_citadel_direct_pdf_missing_response_paths": report.get("mortgage_workflow_replayability_citadel_direct_pdf_missing_response_paths"),
    "mortgage_workflow_replayability_citadel_missing_response_body_count": report.get("mortgage_workflow_replayability_citadel_missing_response_body_count"),
    "mortgage_workflow_replayability_citadel_missing_response_body_path_counts": report.get("mortgage_workflow_replayability_citadel_missing_response_body_path_counts"),
    "mortgage_workflow_capture_queue_rc": report.get("mortgage_workflow_capture_queue_rc"),
    "mortgage_workflow_capture_queue_report": report.get("mortgage_workflow_capture_queue_report"),
    "mortgage_workflow_capture_queue_markdown": report.get("mortgage_workflow_capture_queue_markdown"),
    "mortgage_workflow_capture_queue_idempotency_digest": report.get("mortgage_workflow_capture_queue_idempotency_digest"),
    "mortgage_workflow_capture_queue_status": report.get("mortgage_workflow_capture_queue_status"),
    "mortgage_workflow_capture_queue_reason": report.get("mortgage_workflow_capture_queue_reason"),
    "mortgage_workflow_capture_queue_candidate_capture_handoff_count": report.get("mortgage_workflow_capture_queue_candidate_capture_handoff_count"),
    "mortgage_workflow_capture_queue_downloader_gap_candidate_capture_count": report.get("mortgage_workflow_capture_queue_downloader_gap_candidate_capture_count"),
    "mortgage_workflow_capture_queue_downloader_gap_candidate_capture_properties": report.get("mortgage_workflow_capture_queue_downloader_gap_candidate_capture_properties"),
    "mortgage_workflow_capture_queue_capture_count": report.get("mortgage_workflow_capture_queue_capture_count"),
    "mortgage_workflow_capture_queue_capture_properties": report.get("mortgage_workflow_capture_queue_capture_properties"),
    "mortgage_workflow_capture_queue_duplicate_capture_handoff_count": report.get("mortgage_workflow_capture_queue_duplicate_capture_handoff_count"),
    "mortgage_workflow_capture_queue_duplicate_capture_handoff_properties": report.get("mortgage_workflow_capture_queue_duplicate_capture_handoff_properties"),
    "mortgage_workflow_capture_queue_duplicate_capture_handoff_identities": report.get("mortgage_workflow_capture_queue_duplicate_capture_handoff_identities"),
    "mortgage_workflow_capture_queue_duplicate_capture_handoffs_suppressed": report.get("mortgage_workflow_capture_queue_duplicate_capture_handoffs_suppressed"),
    "mortgage_workflow_capture_queue_missing_har_count": report.get("mortgage_workflow_capture_queue_missing_har_count"),
    "mortgage_workflow_capture_queue_missing_har_properties": report.get("mortgage_workflow_capture_queue_missing_har_properties"),
    "mortgage_workflow_capture_queue_existing_har_needs_bodies_count": report.get("mortgage_workflow_capture_queue_existing_har_needs_bodies_count"),
    "mortgage_workflow_capture_queue_existing_har_needs_bodies_properties": report.get("mortgage_workflow_capture_queue_existing_har_needs_bodies_properties"),
    "mortgage_workflow_capture_queue_existing_har_needs_target_month_statement_pdf_count": report.get("mortgage_workflow_capture_queue_existing_har_needs_target_month_statement_pdf_count"),
    "mortgage_workflow_capture_queue_existing_har_needs_target_month_statement_pdf_properties": report.get("mortgage_workflow_capture_queue_existing_har_needs_target_month_statement_pdf_properties"),
    "mortgage_workflow_capture_queue_rejected_discovered_har_candidate_count": report.get("mortgage_workflow_capture_queue_rejected_discovered_har_candidate_count"),
    "mortgage_workflow_capture_queue_rejected_discovered_har_candidate_paths": report.get("mortgage_workflow_capture_queue_rejected_discovered_har_candidate_paths"),
    "mortgage_workflow_capture_queue_entries": report.get("mortgage_workflow_capture_queue_entries"),
    "mortgage_workflow_capture_queue_safe_to_run_automatically": report.get("mortgage_workflow_capture_queue_safe_to_run_automatically"),
    "mortgage_workflow_capture_queue_manual_capture_count": report.get("mortgage_workflow_capture_queue_manual_capture_count"),
    "mortgage_workflow_capture_queue_manual_capture_properties": report.get("mortgage_workflow_capture_queue_manual_capture_properties"),
    "mortgage_workflow_capture_queue_automatic_capture_count": report.get("mortgage_workflow_capture_queue_automatic_capture_count"),
    "mortgage_workflow_capture_queue_automatic_capture_properties": report.get("mortgage_workflow_capture_queue_automatic_capture_properties"),
    "mortgage_workflow_capture_queue_tokenomics_blocking_count": report.get("mortgage_workflow_capture_queue_tokenomics_blocking_count"),
    "mortgage_workflow_capture_queue_tokenomics_blocking_properties": report.get("mortgage_workflow_capture_queue_tokenomics_blocking_properties"),
    "mortgage_workflow_capture_queue_tokenomics_non_blocking_count": report.get("mortgage_workflow_capture_queue_tokenomics_non_blocking_count"),
    "mortgage_workflow_capture_queue_tokenomics_non_blocking_properties": report.get("mortgage_workflow_capture_queue_tokenomics_non_blocking_properties"),
    "mortgage_workflow_capture_queue_tokenomics_workbook_write_blocker_count": report.get("mortgage_workflow_capture_queue_tokenomics_workbook_write_blocker_count"),
    "mortgage_workflow_capture_queue_tokenomics_workbook_write_blocker_properties": report.get("mortgage_workflow_capture_queue_tokenomics_workbook_write_blocker_properties"),
    "mortgage_workflow_capture_queue_tokenomics_workbook_write_blocker_reasons": report.get("mortgage_workflow_capture_queue_tokenomics_workbook_write_blocker_reasons"),
    "mortgage_workflow_capture_queue_credential_login_hint_mismatch_properties": report.get("mortgage_workflow_capture_queue_credential_login_hint_mismatch_properties"),
    "mortgage_workflow_capture_queue_credential_login_hint_mismatch_overridden_properties": report.get("mortgage_workflow_capture_queue_credential_login_hint_mismatch_overridden_properties"),
    "mortgage_workflow_credential_repair_action_count": report.get("mortgage_workflow_credential_repair_action_count"),
    "mortgage_workflow_credential_repair_properties": report.get("mortgage_workflow_credential_repair_properties"),
    "mortgage_workflow_credential_repair_actions": report.get("mortgage_workflow_credential_repair_actions"),
    "mortgage_workflow_portal_auth_repair_action_count": report.get("mortgage_workflow_portal_auth_repair_action_count"),
    "mortgage_workflow_portal_auth_repair_properties": report.get("mortgage_workflow_portal_auth_repair_properties"),
    "mortgage_workflow_portal_auth_repair_actions": report.get("mortgage_workflow_portal_auth_repair_actions"),
    "mortgage_workflow_replayability_credential_repair_action_count": report.get("mortgage_workflow_replayability_credential_repair_action_count"),
    "mortgage_workflow_replayability_credential_repair_properties": report.get("mortgage_workflow_replayability_credential_repair_properties"),
    "mortgage_workflow_replayability_credential_repair_actions": report.get("mortgage_workflow_replayability_credential_repair_actions"),
    "mortgage_workflow_replayability_portal_auth_repair_action_count": report.get("mortgage_workflow_replayability_portal_auth_repair_action_count"),
    "mortgage_workflow_replayability_portal_auth_repair_properties": report.get("mortgage_workflow_replayability_portal_auth_repair_properties"),
    "mortgage_workflow_replayability_portal_auth_repair_actions": report.get("mortgage_workflow_replayability_portal_auth_repair_actions"),
    "mortgage_workflow_capture_queue_live_cdp_downloader_handoff_count": report.get("mortgage_workflow_capture_queue_live_cdp_downloader_handoff_count"),
    "mortgage_workflow_capture_queue_live_cdp_downloader_handoffs": report.get("mortgage_workflow_capture_queue_live_cdp_downloader_handoffs"),
    "mortgage_workflow_capture_queue_live_cdp_downloader_properties": report.get("mortgage_workflow_capture_queue_live_cdp_downloader_properties"),
    "mortgage_workflow_capture_queue_live_cdp_downloader_commands": report.get("mortgage_workflow_capture_queue_live_cdp_downloader_commands"),
    "mortgage_workflow_capture_queue_processor_rc": report.get("mortgage_workflow_capture_queue_processor_rc"),
    "mortgage_workflow_capture_queue_processor_report": report.get("mortgage_workflow_capture_queue_processor_report"),
    "mortgage_workflow_capture_queue_processor_markdown": report.get("mortgage_workflow_capture_queue_processor_markdown"),
    "mortgage_workflow_capture_queue_processor_idempotency_digest": report.get("mortgage_workflow_capture_queue_processor_idempotency_digest"),
    "mortgage_workflow_capture_queue_processor_status": report.get("mortgage_workflow_capture_queue_processor_status"),
    "mortgage_workflow_capture_queue_processor_reason": report.get("mortgage_workflow_capture_queue_processor_reason"),
    "mortgage_workflow_capture_queue_processor_entry_count": report.get("mortgage_workflow_capture_queue_processor_entry_count"),
    "mortgage_workflow_capture_queue_processor_processed_count": report.get("mortgage_workflow_capture_queue_processor_processed_count"),
    "mortgage_workflow_capture_queue_processor_applied_count": report.get("mortgage_workflow_capture_queue_processor_applied_count"),
    "mortgage_workflow_capture_queue_processor_dry_run_processed_count": report.get("mortgage_workflow_capture_queue_processor_dry_run_processed_count"),
    "mortgage_workflow_capture_queue_processor_apply": report.get("mortgage_workflow_capture_queue_processor_apply"),
    "mortgage_workflow_capture_queue_processor_process_existing_body_recaptures": report.get("mortgage_workflow_capture_queue_processor_process_existing_body_recaptures"),
    "mortgage_workflow_capture_queue_processor_enable_verified_registry_entry": report.get("mortgage_workflow_capture_queue_processor_enable_verified_registry_entry"),
    "mortgage_workflow_capture_queue_processor_pending_apply_count": report.get("mortgage_workflow_capture_queue_processor_pending_apply_count"),
    "mortgage_workflow_capture_queue_processor_pending_apply_properties": report.get("mortgage_workflow_capture_queue_processor_pending_apply_properties"),
    "mortgage_workflow_capture_queue_processor_durable_completion_count": report.get("mortgage_workflow_capture_queue_processor_durable_completion_count"),
    "mortgage_workflow_capture_queue_processor_durable_completion_properties": report.get("mortgage_workflow_capture_queue_processor_durable_completion_properties"),
    "mortgage_workflow_capture_queue_processor_safe_to_run_automatically": report.get("mortgage_workflow_capture_queue_processor_safe_to_run_automatically"),
    "mortgage_workflow_capture_queue_processor_missing_har_count": report.get("mortgage_workflow_capture_queue_processor_missing_har_count"),
    "mortgage_workflow_capture_queue_processor_existing_har_recapture_skipped_count": report.get("mortgage_workflow_capture_queue_processor_existing_har_recapture_skipped_count"),
    "mortgage_workflow_capture_queue_processor_invalid_har_count": report.get("mortgage_workflow_capture_queue_processor_invalid_har_count"),
    "mortgage_workflow_capture_queue_processor_invalid_har_properties": report.get("mortgage_workflow_capture_queue_processor_invalid_har_properties"),
    "mortgage_workflow_capture_queue_processor_auth_blocked_before_capture_count": report.get("mortgage_workflow_capture_queue_processor_auth_blocked_before_capture_count"),
    "mortgage_workflow_capture_queue_processor_auth_blocked_before_capture_properties": report.get("mortgage_workflow_capture_queue_processor_auth_blocked_before_capture_properties"),
    "mortgage_workflow_capture_queue_processor_auth_blocked_capture_requirements": report.get("mortgage_workflow_capture_queue_processor_auth_blocked_capture_requirements"),
    "mortgage_workflow_capture_queue_processor_existing_har_recapture_skipped_properties": report.get("mortgage_workflow_capture_queue_processor_existing_har_recapture_skipped_properties"),
    "mortgage_workflow_capture_queue_processor_existing_har_recapture_requirements": report.get("mortgage_workflow_capture_queue_processor_existing_har_recapture_requirements"),
    "mortgage_workflow_capture_queue_processor_existing_har_target_month_pdf_recapture_skipped_count": report.get("mortgage_workflow_capture_queue_processor_existing_har_target_month_pdf_recapture_skipped_count"),
    "mortgage_workflow_capture_queue_processor_existing_har_target_month_pdf_recapture_skipped_properties": report.get("mortgage_workflow_capture_queue_processor_existing_har_target_month_pdf_recapture_skipped_properties"),
    "mortgage_workflow_capture_queue_processor_existing_har_target_month_pdf_recapture_requirements": report.get("mortgage_workflow_capture_queue_processor_existing_har_target_month_pdf_recapture_requirements"),
    "mortgage_workflow_capture_queue_processor_existing_body_recapture_skipped_count": report.get("mortgage_workflow_capture_queue_processor_existing_body_recapture_skipped_count"),
    "mortgage_workflow_capture_queue_processor_existing_body_recapture_requirements": report.get("mortgage_workflow_capture_queue_processor_existing_body_recapture_requirements"),
    "mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocker_count": report.get("mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocker_count"),
    "mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocker_properties": report.get("mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocker_properties"),
    "mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocker_reasons": report.get("mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocker_reasons"),
    "mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocker_statement_month_statuses": report.get("mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocker_statement_month_statuses"),
    "mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocking_count": report.get("mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocking_count"),
    "mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocking_properties": report.get("mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocking_properties"),
    "mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocking_reasons": report.get("mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocking_reasons"),
    "mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocking_statement_month_statuses": report.get("mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocking_statement_month_statuses"),
    "mortgage_workflow_capture_queue_processor_tokenomics_queue_blocker_count": report.get("mortgage_workflow_capture_queue_processor_tokenomics_queue_blocker_count"),
    "mortgage_workflow_capture_queue_processor_tokenomics_queue_blocker_properties": report.get("mortgage_workflow_capture_queue_processor_tokenomics_queue_blocker_properties"),
    "mortgage_workflow_capture_queue_processor_credential_login_hint_mismatch_properties": report.get("mortgage_workflow_capture_queue_processor_credential_login_hint_mismatch_properties"),
    "mortgage_workflow_capture_queue_processor_credential_login_hint_mismatch_overridden_properties": report.get("mortgage_workflow_capture_queue_processor_credential_login_hint_mismatch_overridden_properties"),
    "mortgage_workflow_capture_queue_processor_queue_blocker_status": report.get("mortgage_workflow_capture_queue_processor_queue_blocker_status"),
    "mortgage_workflow_capture_queue_processor_queue_blocker_reason": report.get("mortgage_workflow_capture_queue_processor_queue_blocker_reason"),
    "mortgage_workflow_capture_queue_processor_queue_blocker_count": report.get("mortgage_workflow_capture_queue_processor_queue_blocker_count"),
    "mortgage_workflow_capture_queue_processor_queue_blocker_properties": report.get("mortgage_workflow_capture_queue_processor_queue_blocker_properties"),
    "mortgage_workflow_capture_queue_processor_operator_next_action_count": report.get("mortgage_workflow_capture_queue_processor_operator_next_action_count"),
    "mortgage_workflow_capture_queue_processor_operator_next_action_properties": report.get("mortgage_workflow_capture_queue_processor_operator_next_action_properties"),
    "mortgage_workflow_capture_queue_processor_operator_next_actions": report.get("mortgage_workflow_capture_queue_processor_operator_next_actions"),
    "mortgage_workflow_capture_queue_processor_portal_auth_repair_action_count": report.get("mortgage_workflow_capture_queue_processor_portal_auth_repair_action_count"),
    "mortgage_workflow_capture_queue_processor_portal_auth_repair_properties": report.get("mortgage_workflow_capture_queue_processor_portal_auth_repair_properties"),
    "mortgage_workflow_capture_queue_processor_portal_auth_repair_actions": report.get("mortgage_workflow_capture_queue_processor_portal_auth_repair_actions"),
    "mortgage_workflow_capture_queue_processor_live_cdp_downloader_handoff_count": report.get("mortgage_workflow_capture_queue_processor_live_cdp_downloader_handoff_count"),
    "mortgage_workflow_capture_queue_processor_live_cdp_downloader_handoffs": report.get("mortgage_workflow_capture_queue_processor_live_cdp_downloader_handoffs"),
    "mortgage_workflow_capture_queue_processor_live_cdp_downloader_properties": report.get("mortgage_workflow_capture_queue_processor_live_cdp_downloader_properties"),
    "mortgage_workflow_capture_queue_processor_live_cdp_downloader_commands": report.get("mortgage_workflow_capture_queue_processor_live_cdp_downloader_commands"),
    "mortgage_workflow_registry_handoff_count": report.get("mortgage_workflow_registry_handoff_count"),
    "mortgage_workflow_registry_handoffs": report.get("mortgage_workflow_registry_handoffs"),
    "mortgage_workflow_registry_handoff_paths": report.get("mortgage_workflow_registry_handoff_paths"),
    "mortgage_workflow_registry_install_commands": report.get("mortgage_workflow_registry_install_commands"),
    "mortgage_workflow_registry_safe_to_register_automatically_count": report.get("mortgage_workflow_registry_safe_to_register_automatically_count"),
    "mortgage_workflow_property_matrix_count": report.get("mortgage_workflow_property_matrix_count"),
    "mortgage_workflow_property_matrix_review_count": report.get("mortgage_workflow_property_matrix_review_count"),
    "mortgage_workflow_property_matrix_manual_action_count": report.get("mortgage_workflow_property_matrix_manual_action_count"),
    "mortgage_workflow_property_matrix_safe_action_count": report.get("mortgage_workflow_property_matrix_safe_action_count"),
    "mortgage_workflow_property_matrix": report.get("mortgage_workflow_property_matrix"),
    "mortgage_workflow_automation_blocked_with_current_statement_count": report.get("mortgage_workflow_automation_blocked_with_current_statement_count"),
    "mortgage_workflow_automation_blocked_with_current_statement_properties": report.get("mortgage_workflow_automation_blocked_with_current_statement_properties"),
    "mortgage_workflow_automation_blocked_with_current_statement_records": report.get("mortgage_workflow_automation_blocked_with_current_statement_records"),
    "mortgage_workflow_current_cycle_future_automation_attention_required": report.get("mortgage_workflow_current_cycle_future_automation_attention_required"),
    "mortgage_workflow_current_cycle_future_automation_attention_count": report.get("mortgage_workflow_current_cycle_future_automation_attention_count"),
    "mortgage_workflow_current_cycle_future_automation_attention_properties": report.get("mortgage_workflow_current_cycle_future_automation_attention_properties"),
    "mortgage_workflow_current_cycle_future_automation_attention_reasons": report.get("mortgage_workflow_current_cycle_future_automation_attention_reasons"),
    "mortgage_workflow_current_cycle_future_automation_attention_records": report.get("mortgage_workflow_current_cycle_future_automation_attention_records"),
    "mortgage_workflow_future_automation_repair_action_count": report.get("mortgage_workflow_future_automation_repair_action_count"),
    "mortgage_workflow_future_automation_repair_properties": report.get("mortgage_workflow_future_automation_repair_properties"),
    "mortgage_workflow_future_automation_repair_reasons": report.get("mortgage_workflow_future_automation_repair_reasons"),
    "mortgage_workflow_future_automation_repair_actions": report.get("mortgage_workflow_future_automation_repair_actions"),
    "mortgage_workflow_tokenomics_workbook_write_blocking_property_count": report.get("mortgage_workflow_tokenomics_workbook_write_blocking_property_count"),
    "mortgage_workflow_tokenomics_workbook_write_blocking_properties": report.get("mortgage_workflow_tokenomics_workbook_write_blocking_properties"),
    "mortgage_workflow_tokenomics_workbook_write_blocking_reasons": report.get("mortgage_workflow_tokenomics_workbook_write_blocking_reasons"),
    "mortgage_workflow_tokenomics_workbook_write_blocking_statement_month_statuses": report.get("mortgage_workflow_tokenomics_workbook_write_blocking_statement_month_statuses"),
    "mortgage_workflow_coownership_tokenomics_status": report.get("mortgage_workflow_coownership_tokenomics_status"),
    "mortgage_workflow_coownership_tokenomics_effective_status": report.get("mortgage_workflow_coownership_tokenomics_effective_status"),
    "mortgage_workflow_coownership_tokenomics_current_month": report.get("mortgage_workflow_coownership_tokenomics_current_month"),
    "mortgage_workflow_coownership_tokenomics_workbook_write_ready": report.get("mortgage_workflow_coownership_tokenomics_workbook_write_ready"),
    "mortgage_workflow_coownership_tokenomics_workbook_write_gate_status": report.get("mortgage_workflow_coownership_tokenomics_workbook_write_gate_status"),
    "mortgage_workflow_coownership_tokenomics_workbook_write_blocker_count": report.get("mortgage_workflow_coownership_tokenomics_workbook_write_blocker_count"),
    "mortgage_workflow_coownership_tokenomics_workbook_write_blocker_properties": report.get("mortgage_workflow_coownership_tokenomics_workbook_write_blocker_properties"),
    "mortgage_workflow_coownership_tokenomics_workbook_write_blocker_reasons": report.get("mortgage_workflow_coownership_tokenomics_workbook_write_blocker_reasons"),
    "mortgage_workflow_coownership_tokenomics_workbook_write_blockers": report.get("mortgage_workflow_coownership_tokenomics_workbook_write_blockers"),
    "mortgage_workflow_coownership_tokenomics_workbook_write_blocking_property_count": report.get("mortgage_workflow_coownership_tokenomics_workbook_write_blocking_property_count"),
    "mortgage_workflow_coownership_tokenomics_workbook_write_blocking_properties": report.get("mortgage_workflow_coownership_tokenomics_workbook_write_blocking_properties"),
    "mortgage_workflow_coownership_tokenomics_workbook_write_ready_property_count": report.get("mortgage_workflow_coownership_tokenomics_workbook_write_ready_property_count"),
    "mortgage_workflow_coownership_tokenomics_workbook_write_ready_properties": report.get("mortgage_workflow_coownership_tokenomics_workbook_write_ready_properties"),
    "mortgage_workflow_coownership_tokenomics_workbook_ready_property_write_plan_count": report.get("mortgage_workflow_coownership_tokenomics_workbook_ready_property_write_plan_count"),
    "mortgage_workflow_coownership_tokenomics_workbook_ready_property_write_plan_properties": report.get("mortgage_workflow_coownership_tokenomics_workbook_ready_property_write_plan_properties"),
    "mortgage_workflow_coownership_tokenomics_workbook_ready_property_write_plan": report.get("mortgage_workflow_coownership_tokenomics_workbook_ready_property_write_plan"),
    "mortgage_workflow_coownership_tokenomics_workbook_ready_property_planned_cell_write_count": report.get("mortgage_workflow_coownership_tokenomics_workbook_ready_property_planned_cell_write_count"),
    "mortgage_workflow_coownership_tokenomics_workbook_ready_property_write_blocked_by_full_gate": report.get("mortgage_workflow_coownership_tokenomics_workbook_ready_property_write_blocked_by_full_gate"),
    "mortgage_workflow_coownership_tokenomics_workbook_partial_ready_property_write_attempted": report.get("mortgage_workflow_coownership_tokenomics_workbook_partial_ready_property_write_attempted"),
    "mortgage_workflow_coownership_tokenomics_workbook_partial_ready_property_write_applied": report.get("mortgage_workflow_coownership_tokenomics_workbook_partial_ready_property_write_applied"),
    "mortgage_workflow_coownership_tokenomics_workbook_partial_ready_property_write_reason": report.get("mortgage_workflow_coownership_tokenomics_workbook_partial_ready_property_write_reason"),
    "mortgage_workflow_coownership_tokenomics_workbook_partial_ready_property_write_property_count": report.get("mortgage_workflow_coownership_tokenomics_workbook_partial_ready_property_write_property_count"),
    "mortgage_workflow_coownership_tokenomics_workbook_partial_ready_property_write_properties": report.get("mortgage_workflow_coownership_tokenomics_workbook_partial_ready_property_write_properties"),
    "mortgage_workflow_coownership_tokenomics_workbook_partial_ready_property_cleared_copied_stale_property_count": report.get("mortgage_workflow_coownership_tokenomics_workbook_partial_ready_property_cleared_copied_stale_property_count"),
    "mortgage_workflow_coownership_tokenomics_workbook_partial_ready_property_cleared_copied_stale_properties": report.get("mortgage_workflow_coownership_tokenomics_workbook_partial_ready_property_cleared_copied_stale_properties"),
    "mortgage_workflow_coownership_tokenomics_blocking_capture_queue_count": report.get("mortgage_workflow_coownership_tokenomics_blocking_capture_queue_count"),
    "mortgage_workflow_coownership_tokenomics_blocking_capture_queue_properties": report.get("mortgage_workflow_coownership_tokenomics_blocking_capture_queue_properties"),
    "mortgage_workflow_coownership_tokenomics_blocking_capture_queue": report.get("mortgage_workflow_coownership_tokenomics_blocking_capture_queue"),
    "mortgage_workflow_downloader_coverage_current_month_statement_gap_count": report.get("mortgage_workflow_downloader_coverage_current_month_statement_gap_count"),
    "mortgage_workflow_downloader_coverage_current_month_statement_gap_properties": report.get("mortgage_workflow_downloader_coverage_current_month_statement_gap_properties"),
    "mortgage_workflow_downloader_coverage_downloader_target_month_statement_gap_count": report.get("mortgage_workflow_downloader_coverage_downloader_target_month_statement_gap_count"),
    "mortgage_workflow_downloader_coverage_downloader_target_month_statement_gap_properties": report.get("mortgage_workflow_downloader_coverage_downloader_target_month_statement_gap_properties"),
    "mortgage_workflow_downloader_coverage_stale_statement_count": report.get("mortgage_workflow_downloader_coverage_stale_statement_count"),
    "mortgage_workflow_downloader_coverage_stale_statement_properties": report.get("mortgage_workflow_downloader_coverage_stale_statement_properties"),
    "mortgage_workflow_downloader_coverage_missing_current_month_statement_count": report.get("mortgage_workflow_downloader_coverage_missing_current_month_statement_count"),
    "mortgage_workflow_downloader_coverage_missing_current_month_statement_properties": report.get("mortgage_workflow_downloader_coverage_missing_current_month_statement_properties"),
    "mortgage_workflow_downloader_coverage_automation_repair_not_statement_gap_count": report.get("mortgage_workflow_downloader_coverage_automation_repair_not_statement_gap_count"),
    "mortgage_workflow_downloader_coverage_automation_repair_not_statement_gap_properties": report.get("mortgage_workflow_downloader_coverage_automation_repair_not_statement_gap_properties"),
    "mortgage_workflow_downloader_coverage_automation_repair_not_statement_gap_downloaders": report.get("mortgage_workflow_downloader_coverage_automation_repair_not_statement_gap_downloaders"),
    "mortgage_workflow_downloader_coverage_unexpected_configured_downloader_count": report.get("mortgage_workflow_downloader_coverage_unexpected_configured_downloader_count"),
    "mortgage_workflow_downloader_coverage_unexpected_configured_downloader_properties": report.get("mortgage_workflow_downloader_coverage_unexpected_configured_downloader_properties"),
    "mortgage_workflow_downloader_coverage_unexpected_configured_downloaders": report.get("mortgage_workflow_downloader_coverage_unexpected_configured_downloaders"),
    "mortgage_workflow_review_packet": report.get("mortgage_workflow_review_packet"),
    "mortgage_downloader_citadel_auth_preflight_report": report.get("mortgage_downloader_citadel_auth_preflight_report"),
    "mortgage_downloader_citadel_auth_preflight_rc": report.get("mortgage_downloader_citadel_auth_preflight_rc"),
    "mortgage_downloader_citadel_auth_preflight_status": report.get("mortgage_downloader_citadel_auth_preflight_status"),
    "mortgage_downloader_citadel_auth_preflight_reason": report.get("mortgage_downloader_citadel_auth_preflight_reason"),
    "mortgage_downloader_citadel_auth_preflight_manual_auth_required": report.get("mortgage_downloader_citadel_auth_preflight_manual_auth_required"),
    "mortgage_downloader_citadel_auth_preflight_candidate_count": report.get("mortgage_downloader_citadel_auth_preflight_candidate_count"),
    "mortgage_downloader_citadel_auth_preflight_route_counts": report.get("mortgage_downloader_citadel_auth_preflight_route_counts"),
    "mortgage_downloader_citadel_auth_preflight_login_tab_count": report.get("mortgage_downloader_citadel_auth_preflight_login_tab_count"),
    "mortgage_downloader_citadel_auth_preflight_non_login_tab_count": report.get("mortgage_downloader_citadel_auth_preflight_non_login_tab_count"),
    "mortgage_downloader_citadel_auth_preflight_scanned_count": report.get("mortgage_downloader_citadel_auth_preflight_scanned_count"),
    "mortgage_downloader_citadel_auth_preflight_authenticated_found": report.get("mortgage_downloader_citadel_auth_preflight_authenticated_found"),
    "mortgage_downloader_citadel_har_preflight_report": report.get("mortgage_downloader_citadel_har_preflight_report"),
    "mortgage_downloader_citadel_har_preflight_rc": report.get("mortgage_downloader_citadel_har_preflight_rc"),
    "mortgage_downloader_citadel_har_preflight_status": report.get("mortgage_downloader_citadel_har_preflight_status"),
    "mortgage_downloader_citadel_har_preflight_capture_quality_status": report.get("mortgage_downloader_citadel_har_preflight_capture_quality_status"),
    "mortgage_downloader_citadel_har_preflight_replay_blocker": report.get("mortgage_downloader_citadel_har_preflight_replay_blocker"),
    "mortgage_downloader_citadel_har_preflight_missing_response_body_count": report.get("mortgage_downloader_citadel_har_preflight_missing_response_body_count"),
    "mortgage_downloader_citadel_har_preflight_missing_response_body_paths": report.get("mortgage_downloader_citadel_har_preflight_missing_response_body_paths"),
    "mortgage_downloader_citadel_har_preflight_missing_response_body_path_counts": report.get("mortgage_downloader_citadel_har_preflight_missing_response_body_path_counts"),
    "mortgage_downloader_citadel_har_preflight_response_body_requirements": report.get("mortgage_downloader_citadel_har_preflight_response_body_requirements"),
    "mortgage_downloader_citadel_har_preflight_replayable_document_payload_count": report.get("mortgage_downloader_citadel_har_preflight_replayable_document_payload_count"),
    "mortgage_downloader_citadel_har_preflight_target_month": report.get("mortgage_downloader_citadel_har_preflight_target_month"),
    "mortgage_downloader_citadel_har_preflight_target_month_replayable_document_available": report.get("mortgage_downloader_citadel_har_preflight_target_month_replayable_document_available"),
    "mortgage_downloader_citadel_har_preflight_target_month_replayable_document_payload_count": report.get("mortgage_downloader_citadel_har_preflight_target_month_replayable_document_payload_count"),
    "mortgage_downloader_citadel_har_preflight_replayable_statement_months": report.get("mortgage_downloader_citadel_har_preflight_replayable_statement_months"),
    "mortgage_downloader_citadel_har_preflight_statement_document_months": report.get("mortgage_downloader_citadel_har_preflight_statement_document_months"),
    "mortgage_downloader_citadel_har_preflight_direct_pdf_response_count": report.get("mortgage_downloader_citadel_har_preflight_direct_pdf_response_count"),
    "mortgage_downloader_citadel_har_preflight_source_direct_pdf_candidate_count": report.get("mortgage_downloader_citadel_har_preflight_source_direct_pdf_candidate_count"),
    "mortgage_downloader_citadel_har_preflight_source_direct_pdf_path_counts": report.get("mortgage_downloader_citadel_har_preflight_source_direct_pdf_path_counts"),
    "mortgage_downloader_citadel_har_preflight_source_direct_pdf_filenames": report.get("mortgage_downloader_citadel_har_preflight_source_direct_pdf_filenames"),
    "mortgage_downloader_citadel_har_preflight_source_direct_pdf_filename_candidates": report.get("mortgage_downloader_citadel_har_preflight_source_direct_pdf_filename_candidates"),
    "mortgage_downloader_citadel_har_preflight_target_month_direct_pdf_filenames": report.get("mortgage_downloader_citadel_har_preflight_target_month_direct_pdf_filenames"),
    "mortgage_downloader_citadel_har_preflight_target_month_direct_pdf_filename_candidates": report.get("mortgage_downloader_citadel_har_preflight_target_month_direct_pdf_filename_candidates"),
    "mortgage_downloader_citadel_har_preflight_target_month_direct_pdf_body_missing_candidate_count": report.get("mortgage_downloader_citadel_har_preflight_target_month_direct_pdf_body_missing_candidate_count"),
    "mortgage_downloader_citadel_har_preflight_source_required_response_candidate_count": report.get("mortgage_downloader_citadel_har_preflight_source_required_response_candidate_count"),
    "mortgage_downloader_citadel_har_preflight_source_required_response_path_counts": report.get("mortgage_downloader_citadel_har_preflight_source_required_response_path_counts"),
    "mortgage_downloader_citadel_har_preflight_direct_pdf_missing_response_count": report.get("mortgage_downloader_citadel_har_preflight_direct_pdf_missing_response_count"),
    "mortgage_downloader_citadel_har_preflight_direct_pdf_missing_response_paths": report.get("mortgage_downloader_citadel_har_preflight_direct_pdf_missing_response_paths"),
    "mortgage_downloader_citadel_har_preflight_replayable_json_response_count": report.get("mortgage_downloader_citadel_har_preflight_replayable_json_response_count"),
    "mortgage_downloader_citadel_har_preflight_embedded_response_body_count": report.get("mortgage_downloader_citadel_har_preflight_embedded_response_body_count"),
    "mortgage_downloader_citadel_har_preflight_embedded_access_token_count": report.get("mortgage_downloader_citadel_har_preflight_embedded_access_token_count"),
    "mortgage_downloader_citadel_har_preflight_can_replay_documents": report.get("mortgage_downloader_citadel_har_preflight_can_replay_documents"),
    "mortgage_downloader_citadel_har_preflight_next_action_status": report.get("mortgage_downloader_citadel_har_preflight_next_action_status"),
    "mortgage_downloader_citadel_har_preflight_next_action_reason": report.get("mortgage_downloader_citadel_har_preflight_next_action_reason"),
    "mortgage_downloader_citadel_har_preflight_next_action_command": report.get("mortgage_downloader_citadel_har_preflight_next_action_command"),
    "mortgage_downloader_citadel_har_preflight_next_action_capture_command": report.get("mortgage_downloader_citadel_har_preflight_next_action_capture_command"),
    "mortgage_downloader_citadel_har_preflight_next_action_capture_required": report.get("mortgage_downloader_citadel_har_preflight_next_action_capture_required"),
    "mortgage_downloader_citadel_har_preflight_next_action_target_month": report.get("mortgage_downloader_citadel_har_preflight_next_action_target_month"),
    "mortgage_downloader_citadel_har_preflight_next_action_target_month_replayable_document_available": report.get("mortgage_downloader_citadel_har_preflight_next_action_target_month_replayable_document_available"),
    "mortgage_downloader_citadel_har_preflight_next_action_target_month_replayable_document_payload_count": report.get("mortgage_downloader_citadel_har_preflight_next_action_target_month_replayable_document_payload_count"),
    "mortgage_downloader_citadel_har_preflight_next_action_replayable_statement_months": report.get("mortgage_downloader_citadel_har_preflight_next_action_replayable_statement_months"),
    "mortgage_downloader_citadel_har_preflight_next_action_statement_document_months": report.get("mortgage_downloader_citadel_har_preflight_next_action_statement_document_months"),
    "mortgage_downloader_citadel_har_preflight_next_action_required_response_paths": report.get("mortgage_downloader_citadel_har_preflight_next_action_required_response_paths"),
    "mortgage_downloader_citadel_har_preflight_next_action_response_body_requirements": report.get("mortgage_downloader_citadel_har_preflight_next_action_response_body_requirements"),
    "mortgage_downloader_citadel_har_preflight_next_action_source_direct_pdf_filenames": report.get("mortgage_downloader_citadel_har_preflight_next_action_source_direct_pdf_filenames"),
    "mortgage_downloader_citadel_har_preflight_next_action_target_month_direct_pdf_filenames": report.get("mortgage_downloader_citadel_har_preflight_next_action_target_month_direct_pdf_filenames"),
    "mortgage_downloader_citadel_har_preflight_next_action_target_month_direct_pdf_body_missing_candidate_count": report.get("mortgage_downloader_citadel_har_preflight_next_action_target_month_direct_pdf_body_missing_candidate_count"),
    "mortgage_downloader_citadel_capture_plan_report": report.get("mortgage_downloader_citadel_capture_plan_report"),
    "mortgage_downloader_citadel_capture_plan_markdown": report.get("mortgage_downloader_citadel_capture_plan_markdown"),
    "mortgage_downloader_citadel_capture_plan_status": report.get("mortgage_downloader_citadel_capture_plan_status"),
    "mortgage_downloader_citadel_capture_plan_latest_live_auth_report": report.get("mortgage_downloader_citadel_capture_plan_latest_live_auth_report"),
    "mortgage_downloader_citadel_capture_plan_latest_live_auth_status": report.get("mortgage_downloader_citadel_capture_plan_latest_live_auth_status"),
    "mortgage_downloader_citadel_capture_plan_latest_live_auth_blocker": report.get("mortgage_downloader_citadel_capture_plan_latest_live_auth_blocker"),
    "mortgage_downloader_citadel_capture_plan_latest_live_auth_auth_failure_reason": report.get("mortgage_downloader_citadel_capture_plan_latest_live_auth_auth_failure_reason"),
    "mortgage_downloader_citadel_capture_plan_latest_live_auth_auth_failure_visible_reason": report.get("mortgage_downloader_citadel_capture_plan_latest_live_auth_auth_failure_visible_reason"),
    "mortgage_downloader_citadel_capture_plan_latest_live_auth_manual_auth_required": report.get("mortgage_downloader_citadel_capture_plan_latest_live_auth_manual_auth_required"),
    "mortgage_downloader_citadel_capture_plan_latest_live_auth_manual_auth_file": report.get("mortgage_downloader_citadel_capture_plan_latest_live_auth_manual_auth_file"),
    "mortgage_downloader_citadel_capture_plan_latest_live_auth_oauth_password_grant_error_codes": report.get("mortgage_downloader_citadel_capture_plan_latest_live_auth_oauth_password_grant_error_codes"),
    "mortgage_downloader_citadel_capture_plan_capture_precondition_status": report.get("mortgage_downloader_citadel_capture_plan_capture_precondition_status"),
    "mortgage_downloader_citadel_capture_plan_capture_precondition_blocker": report.get("mortgage_downloader_citadel_capture_plan_capture_precondition_blocker"),
    "mortgage_downloader_citadel_capture_plan_capture_precondition_reason": report.get("mortgage_downloader_citadel_capture_plan_capture_precondition_reason"),
    "mortgage_downloader_citadel_capture_plan_capture_precondition_manual_auth_required": report.get("mortgage_downloader_citadel_capture_plan_capture_precondition_manual_auth_required"),
    "mortgage_downloader_citadel_capture_plan_capture_precondition_next_action": report.get("mortgage_downloader_citadel_capture_plan_capture_precondition_next_action"),
    "mortgage_downloader_citadel_capture_plan_capture_precondition_safe_to_capture_now": report.get("mortgage_downloader_citadel_capture_plan_capture_precondition_safe_to_capture_now"),
    "mortgage_downloader_citadel_capture_plan_capture_precondition_prepare_command": report.get("mortgage_downloader_citadel_capture_plan_capture_precondition_prepare_command"),
    "mortgage_downloader_citadel_capture_plan_capture_precondition_safe_to_prepare_now": report.get("mortgage_downloader_citadel_capture_plan_capture_precondition_safe_to_prepare_now"),
    "mortgage_downloader_citadel_capture_plan_target_month": report.get("mortgage_downloader_citadel_capture_plan_target_month"),
    "mortgage_downloader_citadel_capture_plan_target_month_replayable_document_available": report.get("mortgage_downloader_citadel_capture_plan_target_month_replayable_document_available"),
    "mortgage_downloader_citadel_capture_plan_target_month_replayable_document_payload_count": report.get("mortgage_downloader_citadel_capture_plan_target_month_replayable_document_payload_count"),
    "mortgage_downloader_citadel_capture_plan_replayable_statement_months": report.get("mortgage_downloader_citadel_capture_plan_replayable_statement_months"),
    "mortgage_downloader_citadel_capture_plan_statement_document_months": report.get("mortgage_downloader_citadel_capture_plan_statement_document_months"),
    "mortgage_downloader_citadel_capture_plan_capture_required": report.get("mortgage_downloader_citadel_capture_plan_capture_required"),
    "mortgage_downloader_citadel_capture_plan_capture_command": report.get("mortgage_downloader_citadel_capture_plan_capture_command"),
    "mortgage_downloader_citadel_capture_plan_next_command": report.get("mortgage_downloader_citadel_capture_plan_next_command"),
    "mortgage_downloader_citadel_capture_plan_capture_har_path": report.get("mortgage_downloader_citadel_capture_plan_capture_har_path"),
    "mortgage_downloader_citadel_capture_plan_post_capture_diagnostics_command": report.get("mortgage_downloader_citadel_capture_plan_post_capture_diagnostics_command"),
    "mortgage_downloader_citadel_capture_plan_post_capture_check_script_path": report.get("mortgage_downloader_citadel_capture_plan_post_capture_check_script_path"),
    "mortgage_downloader_citadel_capture_plan_post_capture_check_command": report.get("mortgage_downloader_citadel_capture_plan_post_capture_check_command"),
    "mortgage_downloader_citadel_capture_plan_post_capture_check_safe_to_run_after_har_capture": report.get("mortgage_downloader_citadel_capture_plan_post_capture_check_safe_to_run_after_har_capture"),
    "mortgage_downloader_citadel_capture_plan_validate_existing_capture_command": report.get("mortgage_downloader_citadel_capture_plan_validate_existing_capture_command"),
    "mortgage_downloader_citadel_capture_plan_advance_capture_workflow_evidence_dry_run_command": report.get("mortgage_downloader_citadel_capture_plan_advance_capture_workflow_evidence_dry_run_command"),
    "mortgage_downloader_citadel_capture_plan_advance_capture_workflow_evidence_apply_command": report.get("mortgage_downloader_citadel_capture_plan_advance_capture_workflow_evidence_apply_command"),
    "mortgage_downloader_citadel_capture_plan_workflow_evidence_advance_report": report.get("mortgage_downloader_citadel_capture_plan_workflow_evidence_advance_report"),
    "mortgage_downloader_citadel_capture_plan_advance_workflow_evidence_command": report.get("mortgage_downloader_citadel_capture_plan_advance_workflow_evidence_command"),
    "mortgage_downloader_citadel_capture_plan_advance_workflow_evidence_apply_command": report.get("mortgage_downloader_citadel_capture_plan_advance_workflow_evidence_apply_command"),
    "mortgage_downloader_citadel_capture_plan_required_response_paths": report.get("mortgage_downloader_citadel_capture_plan_required_response_paths"),
    "mortgage_downloader_citadel_capture_plan_required_response_path_counts": report.get("mortgage_downloader_citadel_capture_plan_required_response_path_counts"),
    "mortgage_downloader_citadel_capture_plan_response_body_requirements": report.get("mortgage_downloader_citadel_capture_plan_response_body_requirements"),
    "mortgage_downloader_citadel_capture_plan_source_direct_pdf_candidate_count": report.get("mortgage_downloader_citadel_capture_plan_source_direct_pdf_candidate_count"),
    "mortgage_downloader_citadel_capture_plan_source_direct_pdf_path_counts": report.get("mortgage_downloader_citadel_capture_plan_source_direct_pdf_path_counts"),
    "mortgage_downloader_citadel_capture_plan_source_direct_pdf_filenames": report.get("mortgage_downloader_citadel_capture_plan_source_direct_pdf_filenames"),
    "mortgage_downloader_citadel_capture_plan_source_direct_pdf_filename_candidates": report.get("mortgage_downloader_citadel_capture_plan_source_direct_pdf_filename_candidates"),
    "mortgage_downloader_citadel_capture_plan_target_month_direct_pdf_filenames": report.get("mortgage_downloader_citadel_capture_plan_target_month_direct_pdf_filenames"),
    "mortgage_downloader_citadel_capture_plan_target_month_direct_pdf_filename_candidates": report.get("mortgage_downloader_citadel_capture_plan_target_month_direct_pdf_filename_candidates"),
    "mortgage_downloader_citadel_capture_plan_target_month_direct_pdf_body_missing_candidate_count": report.get("mortgage_downloader_citadel_capture_plan_target_month_direct_pdf_body_missing_candidate_count"),
    "mortgage_downloader_citadel_capture_plan_source_required_response_candidate_count": report.get("mortgage_downloader_citadel_capture_plan_source_required_response_candidate_count"),
    "mortgage_downloader_citadel_capture_plan_source_required_response_path_counts": report.get("mortgage_downloader_citadel_capture_plan_source_required_response_path_counts"),
    "mortgage_downloader_citadel_capture_plan_safe_to_run_automatically": report.get("mortgage_downloader_citadel_capture_plan_safe_to_run_automatically"),
    "mortgage_downloader_citadel_capture_plan_raw_source_urls_included": report.get("mortgage_downloader_citadel_capture_plan_raw_source_urls_included"),
    "mortgage_downloader_citadel_capture_plan_secrets_included": report.get("mortgage_downloader_citadel_capture_plan_secrets_included"),
    "mortgage_downloader_status": report.get("mortgage_downloader_status"),
    "mortgage_workflow_mortgage_downloader_status": report.get("mortgage_workflow_mortgage_downloader_status"),
    "mortgage_downloader_profile": report.get("mortgage_downloader_profile"),
    "mortgage_workflow_mortgage_downloader_profile": report.get("mortgage_workflow_mortgage_downloader_profile"),
    "mortgage_downloader_target_month": report.get("mortgage_downloader_target_month"),
    "mortgage_workflow_mortgage_downloader_target_month": report.get("mortgage_workflow_mortgage_downloader_target_month"),
    "mortgage_downloader_statement_target_month": report.get("mortgage_downloader_statement_target_month"),
    "mortgage_workflow_mortgage_downloader_statement_target_month": report.get("mortgage_workflow_mortgage_downloader_statement_target_month"),
    "mortgage_downloader_enabled_count": report.get("mortgage_downloader_enabled_count"),
    "mortgage_workflow_mortgage_downloader_enabled_count": report.get("mortgage_workflow_mortgage_downloader_enabled_count"),
    "mortgage_downloader_eligible_count": report.get("mortgage_downloader_eligible_count"),
    "mortgage_workflow_mortgage_downloader_eligible_count": report.get("mortgage_workflow_mortgage_downloader_eligible_count"),
    "mortgage_downloader_skipped_non_co_owner_paid_count": report.get("mortgage_downloader_skipped_non_co_owner_paid_count"),
    "mortgage_workflow_mortgage_downloader_skipped_non_co_owner_paid_count": report.get("mortgage_workflow_mortgage_downloader_skipped_non_co_owner_paid_count"),
    "mortgage_downloader_failed_count": report.get("mortgage_downloader_failed_count"),
    "mortgage_workflow_mortgage_downloader_failed_count": report.get("mortgage_workflow_mortgage_downloader_failed_count"),
    "mortgage_workflow_mortgage_downloader_expected_target_months": report.get("mortgage_workflow_mortgage_downloader_expected_target_months"),
    "mortgage_workflow_mortgage_downloader_expected_target_month_count": report.get("mortgage_workflow_mortgage_downloader_expected_target_month_count"),
    "mortgage_workflow_mortgage_downloader_effective_statement_target_month": report.get("mortgage_workflow_mortgage_downloader_effective_statement_target_month"),
    "mortgage_workflow_mortgage_downloader_target_month_matches_all_expected_months": report.get("mortgage_workflow_mortgage_downloader_target_month_matches_all_expected_months"),
    "mortgage_workflow_mortgage_downloader_target_month_differs_from_expected_months": report.get("mortgage_workflow_mortgage_downloader_target_month_differs_from_expected_months"),
    "mortgage_workflow_mortgage_downloader_target_month_statement_available_count": report.get("mortgage_downloader_target_month_statement_available_count"),
    "mortgage_workflow_mortgage_downloader_target_month_statement_gap_count": report.get("mortgage_downloader_target_month_statement_gap_count"),
    "mortgage_workflow_mortgage_downloader_target_month_statement_gap_properties": report.get("mortgage_downloader_target_month_statement_gap_properties"),
    "mortgage_workflow_mortgage_downloader_target_month_statement_gaps": report.get("mortgage_downloader_target_month_statement_gaps"),
    "mortgage_workflow_mortgage_downloader_current_cycle_statement_ready": report.get("mortgage_downloader_current_cycle_statement_ready"),
    "mortgage_workflow_mortgage_downloader_current_cycle_statement_ready_count": report.get("mortgage_downloader_current_cycle_statement_ready_count"),
    "mortgage_workflow_mortgage_downloader_current_cycle_statement_blocker_count": report.get("mortgage_downloader_current_cycle_statement_blocker_count"),
    "mortgage_workflow_mortgage_downloader_current_cycle_statement_blocker_properties": report.get("mortgage_downloader_current_cycle_statement_blocker_properties"),
    "mortgage_workflow_mortgage_downloader_current_cycle_future_automation_attention_required": report.get("mortgage_downloader_current_cycle_future_automation_attention_required"),
    "mortgage_workflow_mortgage_downloader_current_cycle_future_automation_attention_properties": report.get("mortgage_downloader_current_cycle_future_automation_attention_properties"),
    "mortgage_workflow_mortgage_downloader_current_cycle_future_automation_attention_reasons": report.get("mortgage_downloader_current_cycle_future_automation_attention_reasons"),
    "mortgage_workflow_mortgage_downloader_failed_target_month_statement_available_count": report.get("mortgage_downloader_failed_target_month_statement_available_count"),
    "mortgage_workflow_mortgage_downloader_failed_target_month_statement_available_properties": report.get("mortgage_downloader_failed_target_month_statement_available_properties"),
    "mortgage_workflow_mortgage_downloader_failed_target_month_statement_available_details": report.get("mortgage_downloader_failed_target_month_statement_available_details"),
    "mortgage_workflow_mortgage_downloader_safe_to_run_automatically": report.get("mortgage_downloader_safe_to_run_automatically"),
    "mortgage_workflow_mortgage_downloader_safe_downloader_count": report.get("mortgage_downloader_safe_downloader_count"),
    "mortgage_workflow_mortgage_downloader_unsafe_downloader_count": report.get("mortgage_downloader_unsafe_downloader_count"),
    "mortgage_workflow_mortgage_downloader_unsafe_downloader_properties": report.get("mortgage_downloader_unsafe_downloader_properties"),
    "mortgage_workflow_mortgage_downloader_unsafe_downloader_details": report.get("mortgage_downloader_unsafe_downloader_details"),
    "mortgage_workflow_mortgage_downloader_automation_attention_count": report.get("mortgage_downloader_automation_attention_count"),
    "mortgage_workflow_mortgage_downloader_automation_attention_properties": report.get("mortgage_downloader_automation_attention_properties"),
    "mortgage_workflow_mortgage_downloader_automation_attention_reasons": report.get("mortgage_downloader_automation_attention_reasons"),
    "mortgage_workflow_mortgage_downloader_automation_attention_details": report.get("mortgage_downloader_automation_attention_details"),
    "mortgage_workflow_mortgage_downloader_results": report.get("mortgage_downloader_results"),
    "mortgage_workflow_mortgage_downloader_summaries": report.get("mortgage_downloader_summaries"),
    "mortgage_workflow_mortgage_downloader_status_counts": report.get("mortgage_downloader_status_counts"),
    "mortgage_downloader_target_month_statement_available_count": report.get("mortgage_downloader_target_month_statement_available_count"),
    "mortgage_downloader_expected_target_months": report.get("mortgage_downloader_expected_target_months"),
    "mortgage_downloader_expected_target_month_count": report.get("mortgage_downloader_expected_target_month_count"),
    "mortgage_downloader_effective_statement_target_month": report.get("mortgage_downloader_effective_statement_target_month"),
    "mortgage_downloader_target_month_matches_all_expected_months": report.get("mortgage_downloader_target_month_matches_all_expected_months"),
    "mortgage_downloader_target_month_differs_from_expected_months": report.get("mortgage_downloader_target_month_differs_from_expected_months"),
    "mortgage_downloader_target_month_statement_gap_count": report.get("mortgage_downloader_target_month_statement_gap_count"),
    "mortgage_downloader_target_month_statement_gap_properties": report.get("mortgage_downloader_target_month_statement_gap_properties"),
    "mortgage_downloader_target_month_statement_gaps": report.get("mortgage_downloader_target_month_statement_gaps"),
    "mortgage_downloader_current_cycle_statement_ready": report.get("mortgage_downloader_current_cycle_statement_ready"),
    "mortgage_downloader_current_cycle_statement_ready_count": report.get("mortgage_downloader_current_cycle_statement_ready_count"),
    "mortgage_downloader_current_cycle_statement_blocker_count": report.get("mortgage_downloader_current_cycle_statement_blocker_count"),
    "mortgage_downloader_current_cycle_statement_blocker_properties": report.get("mortgage_downloader_current_cycle_statement_blocker_properties"),
    "mortgage_downloader_current_cycle_future_automation_attention_required": report.get("mortgage_downloader_current_cycle_future_automation_attention_required"),
    "mortgage_downloader_current_cycle_future_automation_attention_properties": report.get("mortgage_downloader_current_cycle_future_automation_attention_properties"),
    "mortgage_downloader_current_cycle_future_automation_attention_reasons": report.get("mortgage_downloader_current_cycle_future_automation_attention_reasons"),
    "mortgage_downloader_failed_target_month_statement_available_count": report.get("mortgage_downloader_failed_target_month_statement_available_count"),
    "mortgage_downloader_failed_target_month_statement_available_properties": report.get("mortgage_downloader_failed_target_month_statement_available_properties"),
    "mortgage_downloader_failed_target_month_statement_available_details": report.get("mortgage_downloader_failed_target_month_statement_available_details"),
    "mortgage_downloader_safe_to_run_automatically": report.get("mortgage_downloader_safe_to_run_automatically"),
    "mortgage_downloader_safe_downloader_count": report.get("mortgage_downloader_safe_downloader_count"),
    "mortgage_downloader_unsafe_downloader_count": report.get("mortgage_downloader_unsafe_downloader_count"),
    "mortgage_downloader_unsafe_downloader_properties": report.get("mortgage_downloader_unsafe_downloader_properties"),
    "mortgage_downloader_unsafe_downloader_details": report.get("mortgage_downloader_unsafe_downloader_details"),
    "mortgage_downloader_automation_attention_count": report.get("mortgage_downloader_automation_attention_count"),
    "mortgage_downloader_automation_attention_properties": report.get("mortgage_downloader_automation_attention_properties"),
    "mortgage_downloader_automation_attention_reasons": report.get("mortgage_downloader_automation_attention_reasons"),
    "mortgage_downloader_automation_attention_details": report.get("mortgage_downloader_automation_attention_details"),
    "mortgage_downloader_results": report.get("mortgage_downloader_results"),
    "mortgage_downloader_summaries": report.get("mortgage_downloader_summaries"),
    "mortgage_downloader_status_counts": report.get("mortgage_downloader_status_counts"),
    "mortgage_downloader_citadel_prepare_rc": report.get("mortgage_downloader_citadel_prepare_rc"),
    "mortgage_downloader_citadel_prepare_status": report.get("mortgage_downloader_citadel_prepare_status"),
    "mortgage_downloader_citadel_prepare_reason": report.get("mortgage_downloader_citadel_prepare_reason"),
    "mortgage_downloader_citadel_prepare_successful_password_request_found": report.get("mortgage_downloader_citadel_prepare_successful_password_request_found"),
    "mortgage_downloader_citadel_prepare_bw_session_status": report.get("mortgage_downloader_citadel_prepare_bw_session_status"),
    "mortgage_downloader_citadel_prepare_bw_item_found": report.get("mortgage_downloader_citadel_prepare_bw_item_found"),
    "mortgage_downloader_citadel_prepare_bw_item_name": report.get("mortgage_downloader_citadel_prepare_bw_item_name"),
    "mortgage_downloader_citadel_prepare_bw_item_uri_host_match": report.get("mortgage_downloader_citadel_prepare_bw_item_uri_host_match"),
    "mortgage_downloader_citadel_prepare_username_matches_har": report.get("mortgage_downloader_citadel_prepare_username_matches_har"),
    "mortgage_downloader_citadel_prepare_password_matched_before_update": report.get("mortgage_downloader_citadel_prepare_password_matched_before_update"),
    "mortgage_downloader_citadel_prepare_password_updated": report.get("mortgage_downloader_citadel_prepare_password_updated"),
    "mortgage_downloader_citadel_prepare_bw_sync_attempted": report.get("mortgage_downloader_citadel_prepare_bw_sync_attempted"),
    "mortgage_downloader_citadel_auth_retryable_portal_failure": report.get("mortgage_downloader_citadel_auth_retryable_portal_failure"),
    "mortgage_downloader_citadel_auth_retryable_reason": report.get("mortgage_downloader_citadel_auth_retryable_reason"),
    "mortgage_downloader_citadel_capture_report": report.get("mortgage_downloader_citadel_capture_report"),
    "mortgage_downloader_citadel_capture_status": report.get("mortgage_downloader_citadel_capture_status"),
    "mortgage_downloader_citadel_capture_reason": report.get("mortgage_downloader_citadel_capture_reason"),
    "mortgage_downloader_citadel_capture_har_token_available": report.get("mortgage_downloader_citadel_capture_har_token_available"),
    "mortgage_downloader_citadel_capture_har_token_entry_count": report.get("mortgage_downloader_citadel_capture_har_token_entry_count"),
    "mortgage_downloader_citadel_capture_har_token_mobile_source_id_available": report.get("mortgage_downloader_citadel_capture_har_token_mobile_source_id_available"),
    "mortgage_downloader_citadel_capture_har_token_source_endpoint_paths": report.get("mortgage_downloader_citadel_capture_har_token_source_endpoint_paths"),
    "mortgage_downloader_citadel_capture_har_token_source_document_detail_id_count": report.get("mortgage_downloader_citadel_capture_har_token_source_document_detail_id_count"),
    "mortgage_downloader_citadel_capture_har_token_endpoint_statuses": report.get("mortgage_downloader_citadel_capture_har_token_endpoint_statuses"),
    "mortgage_downloader_citadel_capture_next_action_status": report.get("mortgage_downloader_citadel_capture_next_action_status"),
    "mortgage_downloader_citadel_capture_next_action_reason": report.get("mortgage_downloader_citadel_capture_next_action_reason"),
    "mortgage_downloader_citadel_capture_next_action_command": report.get("mortgage_downloader_citadel_capture_next_action_command"),
    "mortgage_downloader_citadel_capture_next_action_capture_command": report.get("mortgage_downloader_citadel_capture_next_action_capture_command"),
    "mortgage_downloader_citadel_capture_next_action_capture_required": report.get("mortgage_downloader_citadel_capture_next_action_capture_required"),
    "mortgage_downloader_citadel_capture_next_action_target_month": report.get("mortgage_downloader_citadel_capture_next_action_target_month"),
    "mortgage_downloader_citadel_capture_next_action_target_month_replayable_document_available": report.get("mortgage_downloader_citadel_capture_next_action_target_month_replayable_document_available"),
    "mortgage_downloader_citadel_capture_next_action_target_month_replayable_document_payload_count": report.get("mortgage_downloader_citadel_capture_next_action_target_month_replayable_document_payload_count"),
    "mortgage_downloader_citadel_capture_next_action_replayable_statement_months": report.get("mortgage_downloader_citadel_capture_next_action_replayable_statement_months"),
    "mortgage_downloader_citadel_capture_next_action_statement_document_months": report.get("mortgage_downloader_citadel_capture_next_action_statement_document_months"),
    "mortgage_downloader_citadel_capture_next_action_required_response_paths": report.get("mortgage_downloader_citadel_capture_next_action_required_response_paths"),
    "mortgage_downloader_citadel_capture_next_action_required_response_path_counts": report.get("mortgage_downloader_citadel_capture_next_action_required_response_path_counts"),
    "mortgage_downloader_citadel_capture_next_action_required_response_path_progress": report.get("mortgage_downloader_citadel_capture_next_action_required_response_path_progress"),
    "mortgage_downloader_citadel_capture_next_action_response_body_requirements": report.get("mortgage_downloader_citadel_capture_next_action_response_body_requirements"),
    "mortgage_downloader_citadel_capture_manual_auth_required": report.get("mortgage_downloader_citadel_capture_manual_auth_required"),
    "mortgage_downloader_citadel_capture_manual_auth_file": report.get("mortgage_downloader_citadel_capture_manual_auth_file"),
    "mortgage_downloader_citadel_capture_manual_auth_portal_url": report.get("mortgage_downloader_citadel_capture_manual_auth_portal_url"),
    "mortgage_downloader_citadel_capture_manual_auth_target_id": report.get("mortgage_downloader_citadel_capture_manual_auth_target_id"),
    "mortgage_downloader_citadel_capture_manual_auth_next_command": report.get("mortgage_downloader_citadel_capture_manual_auth_next_command"),
    "mortgage_downloader_citadel_capture_manual_auth_install_verified_har_dry_run_command": report.get("mortgage_downloader_citadel_capture_manual_auth_install_verified_har_dry_run_command"),
    "mortgage_downloader_citadel_capture_manual_auth_install_verified_har_apply_command": report.get("mortgage_downloader_citadel_capture_manual_auth_install_verified_har_apply_command"),
    "mortgage_downloader_citadel_capture_authenticated_found": report.get("mortgage_downloader_citadel_capture_authenticated_found"),
    "mortgage_downloader_citadel_capture_candidate_count": report.get("mortgage_downloader_citadel_capture_candidate_count"),
    "mortgage_downloader_citadel_capture_route_counts": report.get("mortgage_downloader_citadel_capture_route_counts"),
    "mortgage_downloader_citadel_capture_login_tab_count": report.get("mortgage_downloader_citadel_capture_login_tab_count"),
    "mortgage_downloader_citadel_capture_non_login_tab_count": report.get("mortgage_downloader_citadel_capture_non_login_tab_count"),
    "mortgage_downloader_citadel_capture_scanned_count": report.get("mortgage_downloader_citadel_capture_scanned_count"),
    "mortgage_downloader_citadel_capture_captured_endpoint_count": report.get("mortgage_downloader_citadel_capture_captured_endpoint_count"),
    "mortgage_downloader_citadel_capture_captured_response_body_count": report.get("mortgage_downloader_citadel_capture_captured_response_body_count"),
    "mortgage_downloader_citadel_capture_source_har_path_exists": report.get("mortgage_downloader_citadel_capture_source_har_path_exists"),
    "mortgage_downloader_citadel_capture_source_direct_pdf_limit": report.get("mortgage_downloader_citadel_capture_source_direct_pdf_limit"),
    "mortgage_downloader_citadel_capture_source_direct_pdf_candidate_count": report.get("mortgage_downloader_citadel_capture_source_direct_pdf_candidate_count"),
    "mortgage_downloader_citadel_capture_source_direct_pdf_path_counts": report.get("mortgage_downloader_citadel_capture_source_direct_pdf_path_counts"),
    "mortgage_downloader_citadel_capture_source_direct_pdf_fetched_count": report.get("mortgage_downloader_citadel_capture_source_direct_pdf_fetched_count"),
    "mortgage_downloader_citadel_capture_source_direct_pdf_replayable_count": report.get("mortgage_downloader_citadel_capture_source_direct_pdf_replayable_count"),
    "mortgage_downloader_citadel_capture_source_required_response_candidate_count": report.get("mortgage_downloader_citadel_capture_source_required_response_candidate_count"),
    "mortgage_downloader_citadel_capture_source_required_response_path_counts": report.get("mortgage_downloader_citadel_capture_source_required_response_path_counts"),
    "mortgage_downloader_citadel_capture_source_required_response_fetched_count": report.get("mortgage_downloader_citadel_capture_source_required_response_fetched_count"),
    "mortgage_downloader_citadel_capture_source_required_response_replayable_count": report.get("mortgage_downloader_citadel_capture_source_required_response_replayable_count"),
    "mortgage_downloader_citadel_capture_required_response_paths": report.get("mortgage_downloader_citadel_capture_required_response_paths"),
    "mortgage_downloader_citadel_capture_required_response_path_counts": report.get("mortgage_downloader_citadel_capture_required_response_path_counts"),
    "mortgage_downloader_citadel_capture_required_response_path_progress": report.get("mortgage_downloader_citadel_capture_required_response_path_progress"),
    "mortgage_downloader_citadel_capture_response_body_requirements": report.get("mortgage_downloader_citadel_capture_response_body_requirements"),
    "mortgage_downloader_citadel_capture_response_body_requirement_role_counts": report.get("mortgage_downloader_citadel_capture_response_body_requirement_role_counts"),
    "mortgage_downloader_citadel_capture_captured_response_body_requirement_counts": report.get("mortgage_downloader_citadel_capture_captured_response_body_requirement_counts"),
    "mortgage_downloader_citadel_capture_response_body_requirement_role_capture_counts": report.get("mortgage_downloader_citadel_capture_response_body_requirement_role_capture_counts"),
    "mortgage_downloader_citadel_capture_missing_response_body_requirements": report.get("mortgage_downloader_citadel_capture_missing_response_body_requirements"),
    "mortgage_downloader_citadel_capture_missing_response_body_requirement_count": report.get("mortgage_downloader_citadel_capture_missing_response_body_requirement_count"),
    "mortgage_downloader_citadel_capture_captured_required_response_paths": report.get("mortgage_downloader_citadel_capture_captured_required_response_paths"),
    "mortgage_downloader_citadel_capture_captured_required_response_path_counts": report.get("mortgage_downloader_citadel_capture_captured_required_response_path_counts"),
    "mortgage_downloader_citadel_capture_missing_required_response_paths": report.get("mortgage_downloader_citadel_capture_missing_required_response_paths"),
    "mortgage_downloader_citadel_capture_missing_required_response_path_counts": report.get("mortgage_downloader_citadel_capture_missing_required_response_path_counts"),
    "mortgage_downloader_citadel_capture_statement_candidate_count": report.get("mortgage_downloader_citadel_capture_statement_candidate_count"),
    "mortgage_downloader_citadel_capture_replayable_document_payload_count": report.get("mortgage_downloader_citadel_capture_replayable_document_payload_count"),
    "mortgage_downloader_citadel_capture_target_month": report.get("mortgage_downloader_citadel_capture_target_month"),
    "mortgage_downloader_citadel_capture_target_month_replayable_document_available": report.get("mortgage_downloader_citadel_capture_target_month_replayable_document_available"),
    "mortgage_downloader_citadel_capture_target_month_replayable_document_payload_count": report.get("mortgage_downloader_citadel_capture_target_month_replayable_document_payload_count"),
    "mortgage_downloader_citadel_capture_replayable_statement_months": report.get("mortgage_downloader_citadel_capture_replayable_statement_months"),
    "mortgage_downloader_citadel_capture_statement_document_months": report.get("mortgage_downloader_citadel_capture_statement_document_months"),
    "mortgage_downloader_citadel_capture_direct_pdf_response_count": report.get("mortgage_downloader_citadel_capture_direct_pdf_response_count"),
    "mortgage_downloader_citadel_capture_har_path": report.get("mortgage_downloader_citadel_capture_har_path"),
    "mortgage_downloader_citadel_capture_error_count": report.get("mortgage_downloader_citadel_capture_error_count"),
    "mortgage_downloader_citadel_report_status": report.get("mortgage_downloader_citadel_report_status"),
    "mortgage_downloader_citadel_downloaded_count": report.get("mortgage_downloader_citadel_downloaded_count"),
    "mortgage_downloader_citadel_skipped_count": report.get("mortgage_downloader_citadel_skipped_count"),
    "mortgage_downloader_citadel_target_month": report.get("mortgage_downloader_citadel_target_month"),
    "mortgage_downloader_citadel_target_month_statement_available": report.get("mortgage_downloader_citadel_target_month_statement_available"),
    "mortgage_downloader_citadel_target_month_existing_count": report.get("mortgage_downloader_citadel_target_month_existing_count"),
    "mortgage_downloader_citadel_target_month_downloaded_count": report.get("mortgage_downloader_citadel_target_month_downloaded_count"),
    "mortgage_downloader_citadel_target_month_skipped_count": report.get("mortgage_downloader_citadel_target_month_skipped_count"),
    "mortgage_downloader_citadel_existing_target_month_files": report.get("mortgage_downloader_citadel_existing_target_month_files"),
    "mortgage_downloader_citadel_downloaded_target_month_files": report.get("mortgage_downloader_citadel_downloaded_target_month_files"),
    "mortgage_downloader_citadel_skipped_target_month_files": report.get("mortgage_downloader_citadel_skipped_target_month_files"),
    "mortgage_downloader_citadel_safe_to_run_automatically": report.get("mortgage_downloader_citadel_safe_to_run_automatically"),
    "mortgage_downloader_citadel_idempotent_replay_safe": report.get("mortgage_downloader_citadel_idempotent_replay_safe"),
    "mortgage_downloader_citadel_copy_plan_safe_to_apply_automatically": report.get("mortgage_downloader_citadel_copy_plan_safe_to_apply_automatically"),
    "mortgage_downloader_citadel_har_replay_ready_to_run_automatically": report.get("mortgage_downloader_citadel_har_replay_ready_to_run_automatically"),
    "mortgage_downloader_citadel_automation_readiness_status": report.get("mortgage_downloader_citadel_automation_readiness_status"),
    "mortgage_downloader_citadel_automation_blockers": report.get("mortgage_downloader_citadel_automation_blockers"),
    "mortgage_downloader_citadel_idempotent_skip": report.get("mortgage_downloader_citadel_idempotent_skip"),
    "mortgage_downloader_citadel_idempotent_skip_reason": report.get("mortgage_downloader_citadel_idempotent_skip_reason"),
    "mortgage_downloader_citadel_error_count": report.get("mortgage_downloader_citadel_error_count"),
    "mortgage_downloader_citadel_warning_count": report.get("mortgage_downloader_citadel_warning_count"),
    "mortgage_downloader_citadel_auth_state": report.get("mortgage_downloader_citadel_auth_state"),
    "mortgage_downloader_citadel_credentials_available": report.get("mortgage_downloader_citadel_credentials_available"),
    "mortgage_downloader_citadel_login_mode": report.get("mortgage_downloader_citadel_login_mode"),
    "mortgage_downloader_citadel_otp_required": report.get("mortgage_downloader_citadel_otp_required"),
    "mortgage_downloader_citadel_otp_wait_ms": report.get("mortgage_downloader_citadel_otp_wait_ms"),
    "mortgage_downloader_citadel_otp_file": report.get("mortgage_downloader_citadel_otp_file"),
    "mortgage_downloader_citadel_otp_required_file": report.get("mortgage_downloader_citadel_otp_required_file"),
    "mortgage_downloader_citadel_otp_next_command": report.get("mortgage_downloader_citadel_otp_next_command"),
    "mortgage_downloader_citadel_manual_auth_required": report.get("mortgage_downloader_citadel_manual_auth_required"),
    "mortgage_downloader_citadel_manual_auth_reason": report.get("mortgage_downloader_citadel_manual_auth_reason"),
    "mortgage_downloader_citadel_manual_auth_file": report.get("mortgage_downloader_citadel_manual_auth_file"),
    "mortgage_downloader_citadel_manual_auth_portal_url": report.get("mortgage_downloader_citadel_manual_auth_portal_url"),
    "mortgage_downloader_citadel_auth_failure_reason": report.get("mortgage_downloader_citadel_auth_failure_reason"),
    "mortgage_downloader_citadel_auth_failure_visible_reason": report.get("mortgage_downloader_citadel_auth_failure_visible_reason"),
    "mortgage_downloader_citadel_auth_visible_error": report.get("mortgage_downloader_citadel_auth_visible_error"),
    "mortgage_downloader_citadel_credential_state_drift_suspected": report.get("mortgage_downloader_citadel_credential_state_drift_suspected"),
    "mortgage_downloader_citadel_login_form_last_result": report.get("mortgage_downloader_citadel_login_form_last_result"),
    "mortgage_downloader_citadel_oauth_password_grant_failure_count": report.get("mortgage_downloader_citadel_oauth_password_grant_failure_count"),
    "mortgage_downloader_citadel_oauth_password_grant_error_codes": report.get("mortgage_downloader_citadel_oauth_password_grant_error_codes"),
    "mortgage_downloader_citadel_browser_storage_bearer_token_available": report.get("mortgage_downloader_citadel_browser_storage_bearer_token_available"),
    "mortgage_downloader_citadel_browser_storage_mobile_source_id_available": report.get("mortgage_downloader_citadel_browser_storage_mobile_source_id_available"),
    "mortgage_downloader_citadel_browser_storage_token_candidate_count": report.get("mortgage_downloader_citadel_browser_storage_token_candidate_count"),
    "mortgage_downloader_citadel_api_header_mobile_source_id_available": report.get("mortgage_downloader_citadel_api_header_mobile_source_id_available"),
    "mortgage_downloader_citadel_api_header_authorization_enabled": report.get("mortgage_downloader_citadel_api_header_authorization_enabled"),
    "mortgage_downloader_citadel_direct_auth_status": report.get("mortgage_downloader_citadel_direct_auth_status"),
    "mortgage_downloader_citadel_direct_auth_transport": report.get("mortgage_downloader_citadel_direct_auth_transport"),
    "mortgage_downloader_citadel_direct_browser_fallback_attempted": report.get("mortgage_downloader_citadel_direct_browser_fallback_attempted"),
    "mortgage_downloader_citadel_direct_browser_fallback_authenticated_found": report.get("mortgage_downloader_citadel_direct_browser_fallback_authenticated_found"),
    "mortgage_downloader_citadel_direct_fresh_mfa_source_status": report.get("mortgage_downloader_citadel_direct_fresh_mfa_source_status"),
    "mortgage_downloader_citadel_direct_fresh_recaptcha_token_available": report.get("mortgage_downloader_citadel_direct_fresh_recaptcha_token_available"),
    "mortgage_downloader_citadel_direct_fresh_recaptcha_token_length": report.get("mortgage_downloader_citadel_direct_fresh_recaptcha_token_length"),
    "mortgage_downloader_citadel_direct_recaptcha_eval_stage": report.get("mortgage_downloader_citadel_direct_recaptcha_eval_stage"),
    "mortgage_downloader_citadel_direct_recaptcha_eval_error": report.get("mortgage_downloader_citadel_direct_recaptcha_eval_error"),
    "mortgage_downloader_citadel_direct_recaptcha_token_action": report.get("mortgage_downloader_citadel_direct_recaptcha_token_action"),
    "mortgage_downloader_citadel_direct_recaptcha_action_errors": report.get("mortgage_downloader_citadel_direct_recaptcha_action_errors"),
    "mortgage_downloader_citadel_direct_mfa_process_id_header_available": report.get("mortgage_downloader_citadel_direct_mfa_process_id_header_available"),
    "mortgage_downloader_citadel_direct_mfa_detail_available": report.get("mortgage_downloader_citadel_direct_mfa_detail_available"),
    "mortgage_downloader_citadel_direct_mfa_request_uuid_available": report.get("mortgage_downloader_citadel_direct_mfa_request_uuid_available"),
    "mortgage_downloader_citadel_direct_mfa_request_uuid_source": report.get("mortgage_downloader_citadel_direct_mfa_request_uuid_source"),
    "mortgage_downloader_citadel_direct_no_mfa_handoff_after_password": report.get("mortgage_downloader_citadel_direct_no_mfa_handoff_after_password"),
    "mortgage_downloader_citadel_direct_no_mfa_handoff_reason": report.get("mortgage_downloader_citadel_direct_no_mfa_handoff_reason"),
    "mortgage_downloader_citadel_direct_otp_send_via_type": report.get("mortgage_downloader_citadel_direct_otp_send_via_type"),
    "mortgage_downloader_citadel_direct_cdp_mfa_process_id_header_available": report.get("mortgage_downloader_citadel_direct_cdp_mfa_process_id_header_available"),
    "mortgage_downloader_citadel_direct_oauth_cdp_event_count": report.get("mortgage_downloader_citadel_direct_oauth_cdp_event_count"),
    "mortgage_downloader_citadel_direct_oauth_cdp_last_status": report.get("mortgage_downloader_citadel_direct_oauth_cdp_last_status"),
    "mortgage_downloader_citadel_direct_password_token_request_shape_matches_har_success": report.get("mortgage_downloader_citadel_direct_password_token_request_shape_matches_har_success"),
    "mortgage_downloader_citadel_direct_password_token_request_shape_matches_har_failure": report.get("mortgage_downloader_citadel_direct_password_token_request_shape_matches_har_failure"),
    "mortgage_downloader_citadel_direct_password_token_request_shape_matched_har_statuses": report.get("mortgage_downloader_citadel_direct_password_token_request_shape_matched_har_statuses"),
    "mortgage_downloader_citadel_direct_password_token_request_structure_matches_har_success": report.get("mortgage_downloader_citadel_direct_password_token_request_structure_matches_har_success"),
    "mortgage_downloader_citadel_direct_password_token_request_structure_matches_har_failure": report.get("mortgage_downloader_citadel_direct_password_token_request_structure_matches_har_failure"),
    "mortgage_downloader_citadel_direct_password_token_request_structure_matched_har_statuses": report.get("mortgage_downloader_citadel_direct_password_token_request_structure_matched_har_statuses"),
    "mortgage_downloader_citadel_direct_error_codes": report.get("mortgage_downloader_citadel_direct_error_codes"),
    "mortgage_downloader_citadel_direct_error_categories": report.get("mortgage_downloader_citadel_direct_error_categories"),
    "mortgage_downloader_citadel_credential_item_name": report.get("mortgage_downloader_citadel_credential_item_name"),
    "mortgage_downloader_citadel_credential_item_uri_hosts": report.get("mortgage_downloader_citadel_credential_item_uri_hosts"),
    "mortgage_downloader_citadel_credential_item_portal_host_match": report.get("mortgage_downloader_citadel_credential_item_portal_host_match"),
    "mortgage_downloader_citadel_credential_item_field_names": report.get("mortgage_downloader_citadel_credential_item_field_names"),
    "mortgage_downloader_citadel_credential_item_notes_len": report.get("mortgage_downloader_citadel_credential_item_notes_len"),
    "mortgage_downloader_citadel_credential_login_hint_configured": report.get("mortgage_downloader_citadel_credential_login_hint_configured"),
    "mortgage_downloader_citadel_credential_username_matches_login_hint": report.get("mortgage_downloader_citadel_credential_username_matches_login_hint"),
    "mortgage_downloader_citadel_credential_item_name_matches_login_hint": report.get("mortgage_downloader_citadel_credential_item_name_matches_login_hint"),
    "mortgage_downloader_citadel_credential_field_matches_login_hint": report.get("mortgage_downloader_citadel_credential_field_matches_login_hint"),
    "mortgage_downloader_citadel_credential_username_len": report.get("mortgage_downloader_citadel_credential_username_len"),
    "mortgage_downloader_citadel_credential_password_len": report.get("mortgage_downloader_citadel_credential_password_len"),
    "mortgage_downloader_citadel_credential_username_has_at": report.get("mortgage_downloader_citadel_credential_username_has_at"),
    "mortgage_downloader_citadel_har_token_credential_match_count": report.get("mortgage_downloader_citadel_har_token_credential_match_count"),
    "mortgage_downloader_citadel_har_successful_password_token_match": report.get("mortgage_downloader_citadel_har_successful_password_token_match"),
    "mortgage_downloader_citadel_har_mfa_process_id_header_count": report.get("mortgage_downloader_citadel_har_mfa_process_id_header_count"),
    "mortgage_downloader_citadel_har_token_attempt_statuses": report.get("mortgage_downloader_citadel_har_token_attempt_statuses"),
    "mortgage_downloader_citadel_har_workflow_embedded_response_body_count": report.get("mortgage_downloader_citadel_har_workflow_embedded_response_body_count"),
    "mortgage_downloader_citadel_har_workflow_replayable_json_response_count": report.get("mortgage_downloader_citadel_har_workflow_replayable_json_response_count"),
    "mortgage_downloader_citadel_har_workflow_replayable_document_payload_count": report.get("mortgage_downloader_citadel_har_workflow_replayable_document_payload_count"),
    "mortgage_downloader_citadel_har_workflow_target_month": report.get("mortgage_downloader_citadel_har_workflow_target_month"),
    "mortgage_downloader_citadel_har_workflow_target_month_replayable_document_available": report.get("mortgage_downloader_citadel_har_workflow_target_month_replayable_document_available"),
    "mortgage_downloader_citadel_har_workflow_target_month_replayable_document_payload_count": report.get("mortgage_downloader_citadel_har_workflow_target_month_replayable_document_payload_count"),
    "mortgage_downloader_citadel_har_workflow_replayable_statement_months": report.get("mortgage_downloader_citadel_har_workflow_replayable_statement_months"),
    "mortgage_downloader_citadel_har_workflow_statement_document_months": report.get("mortgage_downloader_citadel_har_workflow_statement_document_months"),
    "mortgage_downloader_citadel_har_workflow_direct_pdf_response_count": report.get("mortgage_downloader_citadel_har_workflow_direct_pdf_response_count"),
    "mortgage_downloader_citadel_har_workflow_source_direct_pdf_candidate_count": report.get("mortgage_downloader_citadel_har_workflow_source_direct_pdf_candidate_count"),
    "mortgage_downloader_citadel_har_workflow_source_direct_pdf_path_counts": report.get("mortgage_downloader_citadel_har_workflow_source_direct_pdf_path_counts"),
    "mortgage_downloader_citadel_har_workflow_source_direct_pdf_filenames": report.get("mortgage_downloader_citadel_har_workflow_source_direct_pdf_filenames"),
    "mortgage_downloader_citadel_har_workflow_source_direct_pdf_filename_candidates": report.get("mortgage_downloader_citadel_har_workflow_source_direct_pdf_filename_candidates"),
    "mortgage_downloader_citadel_har_workflow_target_month_direct_pdf_filenames": report.get("mortgage_downloader_citadel_har_workflow_target_month_direct_pdf_filenames"),
    "mortgage_downloader_citadel_har_workflow_target_month_direct_pdf_filename_candidates": report.get("mortgage_downloader_citadel_har_workflow_target_month_direct_pdf_filename_candidates"),
    "mortgage_downloader_citadel_har_workflow_target_month_direct_pdf_body_missing_candidate_count": report.get("mortgage_downloader_citadel_har_workflow_target_month_direct_pdf_body_missing_candidate_count"),
    "mortgage_downloader_citadel_har_workflow_source_required_response_candidate_count": report.get("mortgage_downloader_citadel_har_workflow_source_required_response_candidate_count"),
    "mortgage_downloader_citadel_har_workflow_source_required_response_path_counts": report.get("mortgage_downloader_citadel_har_workflow_source_required_response_path_counts"),
    "mortgage_downloader_citadel_har_workflow_direct_pdf_missing_response_count": report.get("mortgage_downloader_citadel_har_workflow_direct_pdf_missing_response_count"),
    "mortgage_downloader_citadel_har_workflow_direct_pdf_missing_response_paths": report.get("mortgage_downloader_citadel_har_workflow_direct_pdf_missing_response_paths"),
    "mortgage_downloader_citadel_har_workflow_capture_quality_status": report.get("mortgage_downloader_citadel_har_workflow_capture_quality_status"),
    "mortgage_downloader_citadel_har_workflow_replay_blocker": report.get("mortgage_downloader_citadel_har_workflow_replay_blocker"),
    "mortgage_downloader_citadel_har_workflow_missing_response_body_count": report.get("mortgage_downloader_citadel_har_workflow_missing_response_body_count"),
    "mortgage_downloader_citadel_har_workflow_missing_response_body_paths": report.get("mortgage_downloader_citadel_har_workflow_missing_response_body_paths"),
    "mortgage_downloader_citadel_har_workflow_missing_response_body_path_counts": report.get("mortgage_downloader_citadel_har_workflow_missing_response_body_path_counts"),
    "mortgage_downloader_citadel_har_workflow_response_body_requirements": report.get("mortgage_downloader_citadel_har_workflow_response_body_requirements"),
    "mortgage_downloader_citadel_har_workflow_embedded_access_token_count": report.get("mortgage_downloader_citadel_har_workflow_embedded_access_token_count"),
    "mortgage_downloader_citadel_har_workflow_can_replay_documents": report.get("mortgage_downloader_citadel_har_workflow_can_replay_documents"),
    "mortgage_downloader_citadel_har_workflow_next_action_status": report.get("mortgage_downloader_citadel_har_workflow_next_action_status"),
    "mortgage_downloader_citadel_har_workflow_next_action_reason": report.get("mortgage_downloader_citadel_har_workflow_next_action_reason"),
    "mortgage_downloader_citadel_har_workflow_next_action_command": report.get("mortgage_downloader_citadel_har_workflow_next_action_command"),
    "mortgage_downloader_citadel_har_workflow_next_action_capture_command": report.get("mortgage_downloader_citadel_har_workflow_next_action_capture_command"),
    "mortgage_downloader_citadel_har_workflow_next_action_capture_required": report.get("mortgage_downloader_citadel_har_workflow_next_action_capture_required"),
    "mortgage_downloader_citadel_har_workflow_next_action_target_month": report.get("mortgage_downloader_citadel_har_workflow_next_action_target_month"),
    "mortgage_downloader_citadel_har_workflow_next_action_target_month_replayable_document_available": report.get("mortgage_downloader_citadel_har_workflow_next_action_target_month_replayable_document_available"),
    "mortgage_downloader_citadel_har_workflow_next_action_target_month_replayable_document_payload_count": report.get("mortgage_downloader_citadel_har_workflow_next_action_target_month_replayable_document_payload_count"),
    "mortgage_downloader_citadel_har_workflow_next_action_replayable_statement_months": report.get("mortgage_downloader_citadel_har_workflow_next_action_replayable_statement_months"),
    "mortgage_downloader_citadel_har_workflow_next_action_statement_document_months": report.get("mortgage_downloader_citadel_har_workflow_next_action_statement_document_months"),
    "mortgage_downloader_citadel_har_workflow_next_action_required_response_paths": report.get("mortgage_downloader_citadel_har_workflow_next_action_required_response_paths"),
    "mortgage_downloader_citadel_har_workflow_next_action_response_body_requirements": report.get("mortgage_downloader_citadel_har_workflow_next_action_response_body_requirements"),
    "mortgage_downloader_citadel_har_workflow_next_action_source_direct_pdf_filenames": report.get("mortgage_downloader_citadel_har_workflow_next_action_source_direct_pdf_filenames"),
    "mortgage_downloader_citadel_har_workflow_next_action_target_month_direct_pdf_filenames": report.get("mortgage_downloader_citadel_har_workflow_next_action_target_month_direct_pdf_filenames"),
    "mortgage_downloader_citadel_har_workflow_next_action_target_month_direct_pdf_body_missing_candidate_count": report.get("mortgage_downloader_citadel_har_workflow_next_action_target_month_direct_pdf_body_missing_candidate_count"),
    "mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_report": report.get("mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_report"),
    "mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_dry_run_command": report.get("mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_dry_run_command"),
    "mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_apply_command": report.get("mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_apply_command"),
    "mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_direct_dry_run_command": report.get("mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_direct_dry_run_command"),
    "mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_direct_apply_command": report.get("mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_direct_apply_command"),
    "mortgage_downloader_citadel_install_verified_capture_report": report.get("mortgage_downloader_citadel_install_verified_capture_report"),
    "mortgage_downloader_citadel_install_verified_capture_apply_command": report.get("mortgage_downloader_citadel_install_verified_capture_apply_command"),
    "mortgage_downloader_citadel_install_verified_capture_direct_apply_command": report.get("mortgage_downloader_citadel_install_verified_capture_direct_apply_command"),
    "mortgage_downloader_citadel_tab_scan_candidate_count": report.get("mortgage_downloader_citadel_tab_scan_candidate_count"),
    "mortgage_downloader_citadel_tab_scan_limit": report.get("mortgage_downloader_citadel_tab_scan_limit"),
    "mortgage_downloader_citadel_tab_scan_scanned_count": report.get("mortgage_downloader_citadel_tab_scan_scanned_count"),
    "mortgage_downloader_citadel_tab_scan_skipped_count": report.get("mortgage_downloader_citadel_tab_scan_skipped_count"),
    "mortgage_downloader_citadel_tab_scan_fetch_timeout_ms": report.get("mortgage_downloader_citadel_tab_scan_fetch_timeout_ms"),
    "mortgage_downloader_citadel_tab_scan_target_id_requested": report.get("mortgage_downloader_citadel_tab_scan_target_id_requested"),
    "mortgage_downloader_citadel_tab_scan_target_id_found": report.get("mortgage_downloader_citadel_tab_scan_target_id_found"),
    "mortgage_downloader_citadel_tab_scan_authenticated_found": report.get("mortgage_downloader_citadel_tab_scan_authenticated_found"),
    "mortgage_downloader_citadel_tab_scan_direct_fallback_target_selected": report.get("mortgage_downloader_citadel_tab_scan_direct_fallback_target_selected"),
    "mortgage_downloader_citadel_tab_scan_direct_fallback_target_id_requested": report.get("mortgage_downloader_citadel_tab_scan_direct_fallback_target_id_requested"),
    "mortgage_downloader_citadel_tab_scan_direct_fallback_target_id_found": report.get("mortgage_downloader_citadel_tab_scan_direct_fallback_target_id_found"),
    "mortgage_downloader_citadel_oauth_network_event_count": report.get("mortgage_downloader_citadel_oauth_network_event_count"),
    "mortgage_downloader_citadel_oauth_network_statuses": report.get("mortgage_downloader_citadel_oauth_network_statuses"),
    "mortgage_downloader_citadel_oauth_network_error_codes": report.get("mortgage_downloader_citadel_oauth_network_error_codes"),
    "mortgage_downloader_citadel_oauth_network_request_shape_matched_har_statuses": report.get("mortgage_downloader_citadel_oauth_network_request_shape_matched_har_statuses"),
    "mortgage_downloader_citadel_oauth_network_request_structure_matched_har_statuses": report.get("mortgage_downloader_citadel_oauth_network_request_structure_matched_har_statuses"),
}
report["review_safe_idempotency"] = review_safe_idempotency
alerts = []

def intish(value: object) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0

def compact_list(values: object, limit: int = 4) -> list:
    if not isinstance(values, list):
        return []
    return values[:limit]

def reason_parts() -> list[str]:
    return [part for part in str(report.get("reason") or "").split(";") if part]

def weekly_primary_blocker() -> dict:
    reasons = reason_parts()
    reason_set = set(reasons)
    if report.get("status") == "ok":
        return {
            "id": "weekly_file_updates_clean",
            "blocker": None,
            "artifact": str(report_file),
            "next_action": None,
            "hold": None,
            "evidence": {"review_reasons": reasons},
        }
    if report.get("status") in {"skipped_not_friday", "already_done_for_week"}:
        return {
            "id": "weekly_scheduled_noop",
            "blocker": None,
            "artifact": str(report_file),
            "next_action": None,
            "hold": None,
            "evidence": {
                "review_reasons": reasons,
                "status": report.get("status"),
                "reason": report.get("reason"),
            },
        }
    if report.get("disk_space_preflight_status") == "review" or "disk_space_preflight_review" in reason_set:
        next_action = (
            f"{disk_preflight_next_action} Then rerun bash scripts/baselane_weekly_file_updates_cron.sh."
            if disk_preflight_next_action
            else "Free local Dropbox/Windows disk space, then rerun bash scripts/baselane_weekly_file_updates_cron.sh."
        )
        return {
            "id": "weekly_disk_space_preflight",
            "blocker": "Low local disk space blocks weekly file updates",
            "artifact": str(report.get("disk_space_preflight_report") or root / "reports" / "baselane_weekly_disk_space_preflight_report.json"),
            "next_action": next_action,
            "hold": "weekly/monthly document updates",
            "evidence": {
                "review_reasons": reasons,
                "required_free_mib": report.get("disk_space_preflight_required_free_mib"),
                "disk_space_preflight_next_action": report.get("disk_space_preflight_next_action"),
            },
        }
    if report.get("status") == "failed_scope_guard" or "staging_path_not_allowed" in reason_set:
        return {
            "id": "weekly_scope_guard",
            "blocker": "Weekly source path is outside approved Dropbox financial roots",
            "artifact": str(root / "reports" / "baselane_weekly_scope_guard.json"),
            "next_action": "Fix DROPBOX_ROOT/BASELANE_LEDGER_PATH so Baselane automation only reads Dropbox-sourced financial folders, then rerun bash scripts/baselane_weekly_file_updates_cron.sh.",
            "hold": "weekly/monthly document updates",
            "evidence": {"review_reasons": reasons},
        }
    if report.get("status") == "failed" and (
        "hook_returned_nonzero" in reason_set or intish(report.get("return_code")) != 0
    ):
        if report.get("cf_statement_sync_status") == "failed" or intish(report.get("cf_statement_sync_return_code")) != 0:
            return {
                "id": "weekly_cf_hook_failed",
                "blocker": "Weekly Cash Flow Statement hook failed before downstream gates completed",
                "artifact": str(cf_sync_report),
                "next_action": "Open reports/baselane_weekly_cf_statement_sync_report.json and the weekly cron log, fix the CF hook failure, then rerun bash scripts/baselane_weekly_file_updates_cron.sh.",
                "hold": "Lofty PM publish and investor email",
                "evidence": {
                    "review_reasons": reasons,
                    "return_code": intish(report.get("return_code")),
                    "cf_statement_sync_status": report.get("cf_statement_sync_status"),
                    "cf_statement_sync_return_code": intish(report.get("cf_statement_sync_return_code")),
                    "cf_statement_sync_reason": report.get("cf_statement_sync_reason"),
                },
            }
        return {
            "id": "weekly_hook_failed",
            "blocker": "Weekly file update hook exited nonzero before downstream gates completed",
            "artifact": str(report_file),
            "next_action": "Open reports/baselane_weekly_file_updates_run_report.json and the weekly cron log, fix the hook failure, then rerun bash scripts/baselane_weekly_file_updates_cron.sh.",
            "hold": "weekly/monthly document updates",
            "evidence": {
                "review_reasons": reasons,
                "return_code": intish(report.get("return_code")),
                "stale_downstream_gate_suppressed": True,
            },
        }
    if (
        "future_cf_values_review" in reason_set
        or report.get("future_cf_values_status") not in {None, "ok"}
        or intish(report.get("future_cf_values_changed_cell_count")) > 0
        or intish(report.get("future_cf_values_unreadable_count")) > 0
        or report.get("future_cf_values_apply_status") not in {None, "ok"}
        or intish(report.get("future_cf_values_apply_changed_cell_count")) > 0
        or intish(report.get("future_cf_values_apply_unreadable_count")) > 0
    ):
        return {
            "id": "future_cf_values_review",
            "blocker": "Future Cash Flow Statement values require clearing or verification",
            "artifact": report.get("future_cf_values_apply_report") or report.get("future_cf_values_report") or str(future_cf_values_report),
            "next_action": "Review reports/future_cf_statement_values_apply_report.json and reports/future_cf_statement_values_clear_report.json; verify apply changed_cell_count=0 and audit changed_cell_count=0, then rerun bash scripts/baselane_weekly_file_updates_cron.sh.",
            "hold": "weekly/monthly document updates",
            "evidence": {
                "review_reasons": reasons,
                "changed_cell_count": intish(report.get("future_cf_values_changed_cell_count")),
                "unreadable_count": intish(report.get("future_cf_values_unreadable_count")),
                "apply_changed_cell_count": intish(report.get("future_cf_values_apply_changed_cell_count")),
                "apply_unreadable_count": intish(report.get("future_cf_values_apply_unreadable_count")),
            },
        }
    if "cf_no_gl_property_match_review" in reason_set or intish(report.get("cf_no_gl_property_match_active_monthly_scope_count")) > 0:
        return {
            "id": "cf_no_gl_property_match",
            "blocker": "Cash Flow Statement properties do not all match raw property-split GL data",
            "artifact": report.get("cf_no_gl_property_match_csv") or report.get("cf_no_gl_property_match_report") or str(cf_no_gl_property_match_report),
            "next_action": "Resolve CF/GL property-name mapping gaps from the no-GL-property-match report, then rerun bash scripts/baselane_weekly_file_updates_cron.sh.",
            "hold": "Lofty PM publish and investor email",
            "evidence": {
                "review_reasons": reasons,
                "active_monthly_scope_count": intish(report.get("cf_no_gl_property_match_active_monthly_scope_count")),
                "no_gl_property_match_count": intish(report.get("cf_no_gl_property_match_count")),
            },
        }
    if (
        report.get("cf_statement_sync_effective_status") not in {None, "ok"}
        or ("cf_statement_sync_review" in reason_set and report.get("cf_statement_sync_effective_status") != "ok")
    ):
        return {
            "id": "cf_statement_sync_review",
            "blocker": "Cash Flow Statement sync is not effectively clean",
            "artifact": str(cf_sync_report),
            "next_action": "Fix the CF statement sync blockers, rerun bash scripts/baselane_weekly_file_updates_cron.sh, and require effective_status=ok before publish/email.",
            "hold": "Lofty PM publish and investor email",
            "evidence": {
                "review_reasons": reasons,
                "effective_status": report.get("cf_statement_sync_effective_status"),
                "effective_reason": report.get("cf_statement_sync_effective_reason"),
                "effective_blockers": report.get("cf_statement_sync_effective_blockers") or [],
            },
        }
    if report.get("cf_review_gate_status") not in {None, "ok"} or "cf_review_gate_review" in reason_set:
        return {
            "id": "cf_review_gate",
            "blocker": "Cash Flow Statement review gate still has unresolved actions",
            "artifact": str(cf_review_gate_markdown),
            "next_action": "Resolve the weekly CF review gate actions and rerun bash scripts/baselane_weekly_file_updates_cron.sh.",
            "hold": "Lofty PM publish and investor email",
            "evidence": {
                "review_reasons": reasons,
                "status": report.get("cf_review_gate_status"),
                "blocker_count": intish(report.get("cf_review_gate_blocker_count")),
            },
        }
    if (
        report.get("mortgage_workflow_tokenomics_workbook_write_guard_status") == "blocked"
        or report.get("mortgage_workflow_coownership_tokenomics_workbook_write_ready") is False
        or intish(report.get("mortgage_workflow_coownership_tokenomics_workbook_write_blocker_count")) > 0
        or reason_set.intersection(
            {
                "mortgage_workflow_tokenomics_workbook_write_guard_blocked",
                "coownership_tokenomics_workbook_write_review",
                "coownership_tokenomics_workbook_write_not_ready",
            }
        )
    ):
        blocker_properties = (
            report.get("mortgage_workflow_coownership_tokenomics_workbook_write_blocker_properties")
            or report.get("mortgage_workflow_tokenomics_missing_current_month_statement_properties")
            or report.get("mortgage_workflow_downloader_coverage_current_month_statement_gap_properties")
            or []
        )
        return {
            "id": "mortgage_coownership_tokenomics_guard",
            "blocker": "Mortgage/coownership tokenomics workbook guard is not ready",
            "artifact": str(mortgage_workflow_review_packet),
            "next_action": "Resolve the mortgage/coownership tokenomics gates in reports/baselane_monthly_mortgage_workflow_review_packet.md and reports/mortgage_workflow_capture_queue.md, then rerun bash scripts/baselane_weekly_file_updates_cron.sh.",
            "hold": "Lofty PM publish and investor email",
            "evidence": {
                "review_reasons": reasons,
                "guard_status": report.get("mortgage_workflow_tokenomics_workbook_write_guard_status"),
                "guard_reason": report.get("mortgage_workflow_tokenomics_workbook_write_guard_reason"),
                "blocker_count": intish(report.get("mortgage_workflow_coownership_tokenomics_workbook_write_blocker_count")),
                "blocker_properties": compact_list(blocker_properties),
                "capture_queue_status": report.get("mortgage_workflow_capture_queue_status"),
                "capture_queue_tokenomics_blocking_count": intish(report.get("mortgage_workflow_capture_queue_tokenomics_blocking_count")),
                "capture_queue_tokenomics_workbook_write_blocker_count": intish(report.get("mortgage_workflow_capture_queue_tokenomics_workbook_write_blocker_count")),
                "capture_queue_tokenomics_workbook_write_blocker_properties": compact_list(
                    report.get("mortgage_workflow_capture_queue_tokenomics_workbook_write_blocker_properties") or []
                ),
                "capture_queue_tokenomics_workbook_write_blocker_reasons": compact_list(
                    report.get("mortgage_workflow_capture_queue_tokenomics_workbook_write_blocker_reasons") or []
                ),
                "coownership_tokenomics_blocking_capture_queue_count": intish(report.get("mortgage_workflow_coownership_tokenomics_blocking_capture_queue_count")),
                "coownership_tokenomics_blocking_capture_queue_properties": compact_list(
                    report.get("mortgage_workflow_coownership_tokenomics_blocking_capture_queue_properties") or []
                ),
            },
        }
    mortgage_repair_action_count = (
        intish(report.get("mortgage_workflow_credential_repair_action_count"))
        + intish(report.get("mortgage_workflow_portal_auth_repair_action_count"))
        + intish(report.get("mortgage_workflow_replayability_credential_repair_action_count"))
        + intish(report.get("mortgage_workflow_replayability_portal_auth_repair_action_count"))
        + intish(report.get("mortgage_workflow_capture_queue_processor_credential_repair_action_count"))
        + intish(report.get("mortgage_workflow_capture_queue_processor_portal_auth_repair_action_count"))
    )
    if mortgage_repair_action_count > 0:
        credential_properties = (
            report.get("mortgage_workflow_credential_repair_properties")
            or report.get("mortgage_workflow_replayability_credential_repair_properties")
            or report.get("mortgage_workflow_capture_queue_processor_credential_repair_properties")
            or []
        )
        portal_auth_properties = (
            report.get("mortgage_workflow_portal_auth_repair_properties")
            or report.get("mortgage_workflow_replayability_portal_auth_repair_properties")
            or report.get("mortgage_workflow_capture_queue_processor_portal_auth_repair_properties")
            or []
        )
        return {
            "id": "mortgage_workflow_repair_actions",
            "blocker": "Mortgage statement automation has credential or portal-auth repair actions",
            "artifact": str(mortgage_workflow_review_packet),
            "next_action": "Resolve mortgage credential and portal-auth repair actions in reports/baselane_monthly_mortgage_workflow_review_packet.md and reports/mortgage_workflow_repair_packet.json, then rerun bash scripts/baselane_weekly_file_updates_cron.sh.",
            "hold": "mortgage downloader automation parity",
            "evidence": {
                "review_reasons": reasons,
                "credential_repair_action_count": intish(report.get("mortgage_workflow_credential_repair_action_count")),
                "credential_repair_properties": compact_list(credential_properties),
                "portal_auth_repair_action_count": intish(report.get("mortgage_workflow_portal_auth_repair_action_count")),
                "portal_auth_repair_properties": compact_list(portal_auth_properties),
                "replayability_credential_repair_action_count": intish(
                    report.get("mortgage_workflow_replayability_credential_repair_action_count")
                ),
                "replayability_portal_auth_repair_action_count": intish(
                    report.get("mortgage_workflow_replayability_portal_auth_repair_action_count")
                ),
                "capture_queue_processor_credential_repair_action_count": intish(
                    report.get("mortgage_workflow_capture_queue_processor_credential_repair_action_count")
                ),
                "capture_queue_processor_portal_auth_repair_action_count": intish(
                    report.get("mortgage_workflow_capture_queue_processor_portal_auth_repair_action_count")
                ),
            },
        }
    if reason_set.intersection({"ecogl_data_quality_hold", "ecogl_source_fix_queue", "ecogl_source_fix_evidence"}):
        return {
            "id": "ecogl_data_quality",
            "blocker": "ECO GL source data quality is not clean enough for downstream reporting",
            "artifact": str(ecogl_source_fix_action_queue_markdown if ecogl_source_fix_action_queue.exists() else ecogl_autonomy_markdown),
            "next_action": "Fix exact Baselane/ECO GL source categories from approved correction artifacts, export again, and rerun bash scripts/baselane_weekly_file_updates_cron.sh.",
            "hold": "Lofty PM publish and investor email",
            "evidence": {"review_reasons": reasons},
        }
    return {
        "id": "weekly_review",
        "blocker": "Weekly file update run is in review",
        "artifact": str(report_file),
        "next_action": "Open reports/baselane_weekly_file_updates_run_report.json, resolve the listed review_reasons, then rerun bash scripts/baselane_weekly_file_updates_cron.sh.",
        "hold": "weekly/monthly document updates",
        "evidence": {"review_reasons": reasons},
    }

primary_blocker = weekly_primary_blocker()
report["review_reasons"] = reason_parts()
report["primary_blocker"] = primary_blocker
report["next_action"] = primary_blocker.get("next_action")
report["hold"] = primary_blocker.get("hold")
report["actionable_summary"] = {
    "primary_blocker": primary_blocker,
    "review_reason_count": len(report["review_reasons"]),
    "review_reasons": report["review_reasons"],
    "noise_policy": "Use primary_blocker for action; alerts remain diagnostic evidence.",
}

actionable_mortgage_repair_count = (
    intish(report.get("mortgage_workflow_credential_repair_action_count"))
    + intish(report.get("mortgage_workflow_portal_auth_repair_action_count"))
    + intish(report.get("mortgage_workflow_replayability_credential_repair_action_count"))
    + intish(report.get("mortgage_workflow_replayability_portal_auth_repair_action_count"))
    + intish(report.get("mortgage_workflow_capture_queue_processor_credential_repair_action_count"))
    + intish(report.get("mortgage_workflow_capture_queue_processor_portal_auth_repair_action_count"))
)
if actionable_mortgage_repair_count > 0:
    report["actionable_summary"]["mortgage_workflow_repair_actions"] = {
        "action_count": actionable_mortgage_repair_count,
        "artifact": str(mortgage_workflow_review_packet),
        "repair_packet": str(report.get("mortgage_workflow_repair_packet") or root / "reports" / "mortgage_workflow_repair_packet.json"),
        "credential_repair_action_count": intish(report.get("mortgage_workflow_credential_repair_action_count")),
        "credential_repair_properties": compact_list(report.get("mortgage_workflow_credential_repair_properties") or []),
        "portal_auth_repair_action_count": intish(report.get("mortgage_workflow_portal_auth_repair_action_count")),
        "portal_auth_repair_properties": compact_list(report.get("mortgage_workflow_portal_auth_repair_properties") or []),
        "replayability_credential_repair_action_count": intish(
            report.get("mortgage_workflow_replayability_credential_repair_action_count")
        ),
        "replayability_credential_repair_properties": compact_list(
            report.get("mortgage_workflow_replayability_credential_repair_properties") or []
        ),
        "replayability_portal_auth_repair_action_count": intish(
            report.get("mortgage_workflow_replayability_portal_auth_repair_action_count")
        ),
        "replayability_portal_auth_repair_properties": compact_list(
            report.get("mortgage_workflow_replayability_portal_auth_repair_properties") or []
        ),
        "next_action": "Resolve mortgage credential and portal-auth repair actions in reports/baselane_monthly_mortgage_workflow_review_packet.md and reports/mortgage_workflow_repair_packet.json.",
    }
    report["review_safe_idempotency"]["mortgage_workflow_repair_actions"] = report["actionable_summary"][
        "mortgage_workflow_repair_actions"
    ]

def compact_action_plan_item(item):
    if not isinstance(item, dict):
        return None
    parts = [
        f"type={item.get('type') or 'unknown'}",
        f"property={item.get('property') or 'unknown'}",
    ]
    if item.get("next_action_status"):
        parts.append(f"status={item.get('next_action_status')}")
    if item.get("suggested_next_action"):
        parts.append(f"suggested_next_action={item.get('suggested_next_action')}")
    if item.get("reason"):
        parts.append(f"reason={item.get('reason')}")
    for key, label in [
        ("capture_precondition_status", "capture_precondition_status"),
        ("capture_precondition_blocker", "capture_precondition_blocker"),
        ("capture_precondition_reason", "capture_precondition_reason"),
        ("capture_precondition_manual_auth_required", "capture_precondition_manual_auth_required"),
        ("capture_precondition_next_action", "capture_precondition_next_action"),
        ("capture_precondition_safe_to_capture_now", "capture_precondition_safe_to_capture_now"),
        ("capture_precondition_prepare_command", "capture_precondition_prepare_command"),
        ("capture_precondition_safe_to_prepare_now", "capture_precondition_safe_to_prepare_now"),
        ("command_precondition_status", "command_precondition_status"),
        ("command_precondition_reason", "command_precondition_reason"),
        ("command_precondition_next_action", "command_precondition_next_action"),
        ("command_precondition_safe_to_capture_now", "command_precondition_safe_to_capture_now"),
        ("live_cdp_downloader_ready_after_visible_auth", "live_cdp_ready_after_visible_auth"),
        ("credential_login_hint_mismatch", "credential_login_hint_mismatch"),
        ("credential_login_hint_mismatch_overridden", "credential_login_hint_mismatch_overridden"),
        ("target_month_recapture_reason", "target_month_recapture_reason"),
        ("target_month_recapture_required", "target_month_recapture_required"),
    ]:
        if item.get(key) is not None:
            parts.append(f"{label}={item.get(key)}")
    for key, label in [
        ("evidence_sources", "evidence_sources"),
        ("reasons", "reasons"),
        ("duplicate_target_statement_months", "duplicate_target_statement_months"),
    ]:
        value = item.get(key)
        if isinstance(value, list) and value:
            parts.append(f"{label}={value}")
    candidates = item.get("target_month_statement_candidates")
    if isinstance(candidates, list) and candidates:
        statuses = []
        document_ids = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            status = candidate.get("pdf_payload_status")
            if status and status not in statuses:
                statuses.append(status)
            document_id = candidate.get("document_identifier")
            if document_id and document_id not in document_ids:
                document_ids.append(document_id)
        if statuses:
            parts.append(f"target_candidate_pdf_statuses={statuses}")
        if document_ids:
            parts.append(f"target_candidate_document_ids={document_ids}")
    if item.get("next_command"):
        parts.append(f"next_command={item.get('next_command')}")
    if item.get("handoff_command"):
        parts.append(f"handoff_command={item.get('handoff_command')}")
    if item.get("live_cdp_downloader_command"):
        parts.append(f"live_cdp_downloader_command={item.get('live_cdp_downloader_command')}")
    if item.get("handoff_path"):
        parts.append(f"handoff_path={item.get('handoff_path')}")
    if item.get("post_capture_check_command"):
        parts.append(f"post_capture_check={item.get('post_capture_check_command')}")
    if item.get("post_capture_check_script_path"):
        parts.append(f"post_capture_check_script={item.get('post_capture_check_script_path')}")
    for key, label in [
        ("post_capture_check_ready_to_run_now", "post_capture_ready_now"),
        ("register_workflow_evidence_ready_to_run_now", "register_ready_now"),
        ("advance_workflow_evidence_ready_to_run_now", "advance_ready_now"),
        ("capture_required_before_offline_next_step", "capture_first"),
        ("tokenomics_blocking", "tokenomics_blocking"),
        ("workbook_write_blocking", "workbook_write_blocking"),
        ("tokenomics_workbook_write_blocking", "tokenomics_workbook_write_blocking"),
    ]:
        if item.get(key) is not None:
            parts.append(f"{label}={item.get(key)}")
    if item.get("tokenomics_write_blocker_count") is not None:
        parts.append(f"tokenomics_write_blocker_count={item.get('tokenomics_write_blocker_count')}")
    for key, label in [
        ("tokenomics_write_blocker_reasons", "tokenomics_write_blocker_reasons"),
        ("tokenomics_write_blocker_statement_month_statuses", "tokenomics_write_blocker_statuses"),
    ]:
        value = item.get(key)
        if isinstance(value, list) and value:
            parts.append(f"{label}={value}")
    if item.get("register_workflow_evidence_command"):
        parts.append(f"register_workflow_evidence={item.get('register_workflow_evidence_command')}")
    if item.get("register_workflow_evidence_apply_command"):
        parts.append(f"register_workflow_evidence_apply={item.get('register_workflow_evidence_apply_command')}")
    if item.get("advance_workflow_evidence_command"):
        parts.append(f"advance_workflow_evidence={item.get('advance_workflow_evidence_command')}")
    if item.get("advance_workflow_evidence_apply_command"):
        parts.append(f"advance_workflow_evidence_apply={item.get('advance_workflow_evidence_apply_command')}")
    if item.get("analysis_command"):
        parts.append(f"analysis_command={item.get('analysis_command')}")
    if item.get("scaffold_command"):
        parts.append(f"scaffold_command={item.get('scaffold_command')}")
    if item.get("capture_command"):
        parts.append(f"capture_command={item.get('capture_command')}")
    if item.get("report_path"):
        parts.append(f"analysis_report={Path(str(item.get('report_path'))).name}")
    report_paths = item.get("report_paths")
    if isinstance(report_paths, list) and report_paths:
        parts.append(f"analysis_reports={[Path(str(path)).name for path in report_paths]}")
    if item.get("analysis_report"):
        parts.append(f"analysis_report={Path(str(item.get('analysis_report'))).name}")
    if item.get("scaffold_report"):
        parts.append(f"scaffold_report={Path(str(item.get('scaffold_report'))).name}")
    if item.get("manifest_path"):
        parts.append(f"manifest={Path(str(item.get('manifest_path'))).name}")
    if item.get("registry_entry_path"):
        parts.append(f"registry_entry={Path(str(item.get('registry_entry_path'))).name}")
    if item.get("registry_entry_written") is not None:
        parts.append(f"registry_written={item.get('registry_entry_written')}")
    if item.get("registry_install_command"):
        parts.append(f"registry_install_command={item.get('registry_install_command')}")
    if item.get("generated_dry_run_command"):
        parts.append(f"generated_dry_run={item.get('generated_dry_run_command')}")
    if item.get("generated_dry_run_status"):
        parts.append(f"generated_dry_run_status={item.get('generated_dry_run_status')}")
    if item.get("generated_dry_run_verified_target_month") is not None:
        parts.append(f"generated_verified_target_month={item.get('generated_dry_run_verified_target_month')}")
    if item.get("generated_dry_run_target_month_downloadable_count") is not None:
        parts.append(f"generated_downloadable={item.get('generated_dry_run_target_month_downloadable_count')}")
    if item.get("safe_to_register_automatically") is not None:
        parts.append(f"safe_register={item.get('safe_to_register_automatically')}")
    if item.get("safe_to_run_automatically") is not None:
        parts.append(f"safe={item.get('safe_to_run_automatically')}")
    if item.get("suggested_workflow_har_path"):
        parts.append(f"har_path={item.get('suggested_workflow_har_path')}")
    if item.get("har_path"):
        parts.append(f"har_path={item.get('har_path')}")
    har_paths = item.get("har_paths")
    if isinstance(har_paths, list) and har_paths:
        parts.append(f"har_paths={har_paths}")
    if item.get("capture_required") is not None:
        parts.append(f"capture_required={item.get('capture_required')}")
    required_paths = item.get("required_response_paths")
    if isinstance(required_paths, list) and required_paths:
        parts.append(f"required_paths={required_paths}")
    if item.get("portal_url"):
        parts.append(f"portal={item.get('portal_url')}")
    return " ".join(parts)

def compact_coverage_statement_gap_item(item):
    if not isinstance(item, dict):
        return None
    parts = [f"property={item.get('property') or 'unknown'}"]
    for key, label in [
        ("statement_month_status", "statement_month_status"),
        ("remediation_reason", "reason"),
        ("reason", "reason"),
        ("target_month", "target_month"),
        ("suggested_next_action", "suggested_next_action"),
        ("required_capture_quality", "required_capture_quality"),
        ("suggested_workflow_har_path", "har_path"),
        ("suggested_workflow_har_path_exists", "har_exists"),
        ("latest_statement_document_metadata_only_month", "latest_metadata_only_month"),
        ("capture_command_ready_to_run_now", "capture_ready_now"),
        ("capture_command_safe_to_run_automatically", "capture_safe_auto"),
        ("post_capture_check_safe_to_run_after_har_capture", "post_capture_check_safe"),
    ]:
        if item.get(key) is not None:
            parts.append(f"{label}={item.get(key)}")
    if item.get("post_capture_check_command"):
        parts.append(f"post_capture_check={item.get('post_capture_check_command')}")
    return " ".join(parts)

def compact_blocked_downloader_item(item):
    if not isinstance(item, dict):
        return None
    parts = []
    if item.get("property"):
        parts.append(f"property={item.get('property')}")
    for key, label in [
        ("automation_blockers", "blockers"),
        ("har_replay_blockers", "har_blockers"),
        ("har_capture_quality_statuses", "har_quality"),
        ("latest_report_statuses", "latest_statuses"),
        ("next_action_statuses", "statuses"),
        ("next_action_capture_commands", "capture_commands"),
        ("capture_precondition_prepare_commands", "prepare_commands"),
        ("capture_precondition_safe_to_prepare_now", "safe_to_prepare_now"),
        ("next_action_commands", "next_commands"),
    ]:
        value = item.get(key)
        if isinstance(value, list) and value:
            parts.append(f"{label}={value}")
    required_paths = item.get("required_response_paths")
    if isinstance(required_paths, list) and required_paths:
        parts.append(f"required_paths={required_paths}")
    if item.get("capture_required") is not None:
        parts.append(f"capture_required={item.get('capture_required')}")
    return " ".join(parts)

def compact_response_body_requirements(requirements):
    if not isinstance(requirements, list) or not requirements:
        return None
    roles = []
    for item in requirements:
        if not isinstance(item, dict):
            continue
        role = item.get("role") or "unknown"
        if role not in roles:
            roles.append(role)
    return f"count={len(requirements)} roles={roles}"

def compact_property_matrix_item(item):
    if not isinstance(item, dict):
        return None
    parts = []
    for key, label in [
        ("property", "property"),
        ("workflow_status", "status"),
        ("coverage_status", "coverage"),
        ("operational_status", "operational"),
        ("statement_found", "statement_found"),
        ("statement_month_status", "statement_month_status"),
        ("statement_month", "statement_month"),
        ("current_month_statement_available", "current_month_statement_available"),
        ("manual_action_count", "manual"),
        ("safe_action_count", "safe"),
    ]:
        value = item.get(key)
        if value not in (None, "", []):
            parts.append(f"{label}={value}")
    for key, label in [
        ("next_action_types", "actions"),
        ("suggested_next_actions", "suggested_actions"),
        ("next_action_statuses", "action_statuses"),
        ("required_response_paths", "required_paths"),
        ("capture_commands", "capture_commands"),
        ("registry_entry_paths", "registry_entries"),
    ]:
        value = item.get(key)
        if isinstance(value, list) and value:
            parts.append(f"{label}={value}")
    return " ".join(parts)

def compact_future_automation_repair_item(item):
    if not isinstance(item, dict):
        return None
    parts = []
    for key, label in [
        ("property", "property"),
        ("source", "source"),
        ("source_downloader_id", "downloader"),
        ("operational_status", "operational"),
        ("statement_month_status", "statement_month_status"),
        ("current_month_statement_available", "current_available"),
        ("operator_next_action", "operator_next_action"),
        ("credential_lookup_absent", "credential_absent"),
        ("credential_lookup_misfiled_candidate_count", "credential_misfiled_count"),
        ("credential_lookup_scope", "credential_scope"),
        ("credential_lookup_repair_action", "credential_repair_action"),
        ("validation_command", "validation"),
        ("capture_command", "capture"),
    ]:
        value = item.get(key)
        if value not in (None, "", []):
            parts.append(f"{label}={value}")
    reasons = item.get("reasons")
    if isinstance(reasons, list) and reasons:
        parts.append(f"reasons={reasons}")
    return " ".join(parts)

def compact_downloader_summary_item(item):
    if not isinstance(item, dict):
        return None
    parts = []
    for key, label in [
        ("property", "property"),
        ("status", "status"),
        ("report_status", "report_status"),
        ("target_month", "target_month"),
        ("target_month_statement_available", "target_available"),
        ("credentials_available", "credentials_available"),
        ("credential_source", "credential_source"),
        ("credential_lookup_status", "credential_lookup_status"),
        ("credential_lookup_failure_reason", "credential_lookup_failure_reason"),
        ("credential_lookup_item_name", "credential_lookup_item_name"),
        ("credential_lookup_expected_folder_name", "credential_lookup_expected_folder_name"),
        ("credential_lookup_uri_host", "credential_lookup_uri_host"),
        ("credential_login_hint_mismatch", "credential_login_hint_mismatch"),
        ("credential_login_hint_mismatch_overridden", "credential_login_hint_mismatch_overridden"),
        ("credential_lookup_candidate_count", "credential_lookup_candidate_count"),
        ("auth_issue", "auth_issue"),
        ("required_capture_quality", "required_capture_quality"),
        ("suggested_next_action", "suggested_next_action"),
    ]:
        value = item.get(key)
        if value not in (None, "", []):
            parts.append(f"{label}={value}")
    identifiers = item.get("target_month_document_identifiers")
    if isinstance(identifiers, list) and identifiers:
        parts.append(f"target_doc_ids={identifiers}")
    return " ".join(parts)

def compact_workbook_blocker_item(item):
    if not isinstance(item, dict):
        return None
    parts = []
    for key, label in [
        ("property", "property"),
        ("reasons", "reasons"),
        ("workflow_status", "status"),
        ("coverage_status", "coverage"),
        ("operational_status", "operational"),
        ("current_month_statement_available", "current_month_statement_available"),
        ("statement_month_status", "statement_month_status"),
        ("manual_action_count", "manual"),
    ]:
        value = item.get(key)
        if value not in (None, "", []):
            parts.append(f"{label}={value}")
    for key, label in [
        ("next_action_types", "actions"),
        ("next_action_statuses", "action_statuses"),
        ("required_response_paths", "required_paths"),
        ("capture_commands", "capture_commands"),
        ("registry_entry_paths", "registry_entries"),
    ]:
        value = item.get(key)
        if isinstance(value, list) and value:
            parts.append(f"{label}={value}")
    return " ".join(parts)

def suppress_stale_downstream_for_disk_hold() -> None:
    if report.get("disk_space_preflight_status") != "review" and "disk_space_preflight_review" not in reason_parts():
        return
    stale_prefixes = (
        "mortgage_",
        "native_split_",
        "weekly_unprocessed_",
        "weekly_duplicate_",
        "weekly_raw_",
        "weekly_candidate_",
        "weekly_exact_",
        "weekly_notes_",
        "weekly_deduped_",
        "weekly_safe_",
        "weekly_clean_",
        "weekly_accrual_",
        "cf_",
        "ecogl_",
        "first_day_",
        "future_cf_",
    )
    preserved = {
        "mortgage_workflow_script",
        "mortgage_workflow_report",
    }
    removed = []
    for key in list(report):
        if key in preserved:
            continue
        if key == "review_safe_idempotency" or key.startswith(stale_prefixes):
            removed.append(key)
            report.pop(key, None)
    report["stale_downstream_gate_suppressed"] = True
    report["stale_downstream_gate_suppressed_reason"] = "disk_space_preflight_review"
    report["stale_downstream_gate_suppressed_field_count"] = len(removed)
    report["stale_downstream_gate_suppressed_prefixes"] = list(stale_prefixes)
    report["actionable_summary"]["noise_policy"] = "Disk preflight is the only actionable blocker; downstream stale evidence is suppressed until disk is fixed and weekly reruns."

suppress_stale_downstream_for_disk_hold()

def suppress_stale_downstream_for_scheduled_noop() -> None:
    if report.get("status") not in {"skipped_not_friday", "already_done_for_week"}:
        return
    stale_prefixes = (
        "mortgage_",
        "native_split_",
        "weekly_unprocessed_",
        "weekly_duplicate_",
        "weekly_raw_",
        "weekly_candidate_",
        "weekly_exact_",
        "weekly_notes_",
        "weekly_deduped_",
        "weekly_safe_",
        "weekly_clean_",
        "weekly_accrual_",
        "cf_",
        "ecogl_",
        "first_day_",
        "future_cf_",
    )
    preserved = {
        "mortgage_workflow_script",
        "mortgage_workflow_report",
        "mortgage_workflow_status",
        "mortgage_workflow_rc",
    }
    removed = []
    for key in list(report):
        if key in preserved:
            continue
        if key.startswith("mortgage_workflow_") or key.startswith("mortgage_downloader_citadel_"):
            continue
        if key == "review_safe_idempotency" or key.startswith(stale_prefixes):
            removed.append(key)
            report.pop(key, None)
    report["stale_downstream_gate_suppressed"] = True
    report["stale_downstream_gate_suppressed_reason"] = "scheduled_noop"
    report["stale_downstream_gate_suppressed_field_count"] = len(removed)
    report["stale_downstream_gate_suppressed_prefixes"] = list(stale_prefixes)
    report["weekly_unprocessed_idempotent"] = True
    report["weekly_unprocessed_state_idempotent"] = True
    report["cf_statement_sync_status"] = "ok"
    report["cf_statement_sync_return_code"] = 0
    report["cf_statement_sync_source_cash_balance_violation_count"] = 0
    report["cf_statement_sync_no_mortgage_debt_violation_count"] = 0
    report["cf_statement_sync_conflict_count"] = 0
    report["cf_statement_sync_missing_canonical_cf_count"] = 0
    report["cf_review_gate_status"] = "ok"
    report["cf_review_gate_action_queue_count"] = 0
    report["cf_review_gate_blocker_count"] = 0
    report["future_cf_values_status"] = "ok"
    report["future_cf_values_changed_cell_count"] = 0
    report["future_cf_values_unreadable_count"] = 0
    report["review_safe_idempotency"] = {
        "status": report.get("status"),
        "iso_week": report.get("iso_week"),
        "last_completed_week": report.get("last_completed_week"),
        "state_file": report.get("state_file"),
        "state_file_marked_complete": report.get("state_file_marked_complete"),
        "state_file_unmarked": report.get("state_file_unmarked"),
        "state_file_unmarked_reason": report.get("state_file_unmarked_reason"),
        "safe_to_skip_next_run": True,
        "retry_required": False,
        "retry_safe_without_duplicate_outputs": True,
        "weekly_unprocessed_idempotent": True,
        "weekly_unprocessed_state_idempotent": True,
        "deterministic_verification_idempotent": report.get("deterministic_verification_idempotent"),
        "scheduled_noop": True,
        "scheduled_noop_reason": report.get("reason"),
        "mortgage_workflow_report_exists": report.get("mortgage_workflow_report_exists"),
        "mortgage_workflow_report_readable": report.get("mortgage_workflow_report_readable"),
        "mortgage_workflow_gate_status": report.get("mortgage_workflow_gate_status"),
        "mortgage_workflow_replayability_next_action_plan_count": report.get(
            "mortgage_workflow_replayability_next_action_plan_count"
        ),
        "mortgage_workflow_capture_queue_status": report.get("mortgage_workflow_capture_queue_status"),
        "mortgage_workflow_capture_queue_capture_count": report.get("mortgage_workflow_capture_queue_capture_count"),
    }
    report["actionable_summary"]["noise_policy"] = "Scheduled no-op; downstream stale evidence is suppressed until the weekly pass actually runs."

suppress_stale_downstream_for_scheduled_noop()

if report.get("status") == "review":
    reason = report.get("reason")
    if reason:
        alerts.append(f"weekly_review_reason={reason}")
if report.get("mortgage_workflow_gate_status") == "review":
    alerts.append(
        "mortgage_workflow_review "
        f"reason={report.get('mortgage_workflow_gate_reason') or 'unknown'} "
        f"citadel_rc={report.get('mortgage_workflow_citadel_download_rc')}"
    )
if report.get("mortgage_workflow_downloader_coverage_missing_downloader_count"):
    alerts.append(
        "mortgage_downloader_coverage_missing "
        f"count={report.get('mortgage_workflow_downloader_coverage_missing_downloader_count')} "
        f"properties={report.get('mortgage_workflow_downloader_coverage_missing_downloader_properties')}"
    )
if report.get("mortgage_workflow_downloader_coverage_tokenomics_fallback_report_used"):
    alerts.append(
        "mortgage_downloader_coverage_tokenomics_fallback "
        f"source={report.get('mortgage_workflow_downloader_coverage_tokenomics_source')} "
        f"report={report.get('mortgage_workflow_downloader_coverage_tokenomics_fallback_report')}"
    )
if report.get("mortgage_workflow_tokenomics_current_month_statement_gap_count"):
    alerts.append(
        "mortgage_workflow_tokenomics_current_month_statement_gaps "
        f"count={report.get('mortgage_workflow_tokenomics_current_month_statement_gap_count')} "
        f"properties={report.get('mortgage_workflow_tokenomics_current_month_statement_gap_properties')} "
        f"stale={report.get('mortgage_workflow_tokenomics_stale_statement_count')} "
        f"missing={report.get('mortgage_workflow_tokenomics_missing_current_month_statement_count')}"
    )
if report.get("mortgage_workflow_tokenomics_current_month_statement_gap_remediation_count"):
    alerts.append(
        "mortgage_workflow_tokenomics_gap_remediation "
        f"count={report.get('mortgage_workflow_tokenomics_current_month_statement_gap_remediation_count')} "
        f"available={report.get('mortgage_workflow_tokenomics_current_month_statement_gap_remediation_available_count')} "
        f"missing_actions={report.get('mortgage_workflow_tokenomics_current_month_statement_gap_remediation_missing_action_count')} "
        f"missing_action_properties={report.get('mortgage_workflow_tokenomics_current_month_statement_gap_remediation_missing_action_properties')}"
    )
if report.get("mortgage_workflow_tokenomics_workbook_write_guard_status") == "blocked":
    alerts.append(
        "mortgage_workflow_tokenomics_workbook_write_guard "
        f"status={report.get('mortgage_workflow_tokenomics_workbook_write_guard_status')} "
        f"reason={report.get('mortgage_workflow_tokenomics_workbook_write_guard_reason') or 'unknown'} "
        f"downloader_gap_blocking={report.get('mortgage_workflow_downloader_tokenomics_blocking_gap_count')} "
        f"citadel_blocking={report.get('mortgage_workflow_downloader_citadel_tokenomics_blocking')}"
    )
if report.get("mortgage_downloader_target_month_statement_gap_count"):
    alerts.append(
        "mortgage_downloader_target_month_statement_gaps "
        f"count={report.get('mortgage_downloader_target_month_statement_gap_count')} "
        f"properties={report.get('mortgage_downloader_target_month_statement_gap_properties')}"
    )
    downloader_summaries = report.get("mortgage_downloader_summaries")
    if isinstance(downloader_summaries, list):
        for index, item in enumerate(downloader_summaries[:5], start=1):
            compact = compact_downloader_summary_item(item)
            if compact:
                alerts.append(f"mortgage_downloader_summary_{index} {compact}")
if report.get("mortgage_downloader_automation_attention_count"):
    alerts.append(
        "mortgage_downloader_automation_attention "
        f"count={report.get('mortgage_downloader_automation_attention_count')} "
        f"properties={report.get('mortgage_downloader_automation_attention_properties')} "
        f"reasons={report.get('mortgage_downloader_automation_attention_reasons')}"
    )
if report.get("mortgage_workflow_downloader_coverage_blocked_configured_downloader_count"):
    alerts.append(
        "mortgage_downloader_coverage_blocked_configured "
        f"count={report.get('mortgage_workflow_downloader_coverage_blocked_configured_downloader_count')}"
    )
    blocked_downloaders = report.get("mortgage_workflow_downloader_coverage_blocked_configured_downloaders")
    if isinstance(blocked_downloaders, list):
        for index, item in enumerate(blocked_downloaders[:3], start=1):
            compact = compact_blocked_downloader_item(item)
            if compact:
                alerts.append(f"mortgage_downloader_coverage_blocked_configured_{index} {compact}")
if report.get("mortgage_workflow_downloader_coverage_automation_repair_not_statement_gap_count"):
    alerts.append(
        "mortgage_downloader_coverage_automation_repair_not_statement_gap "
        f"count={report.get('mortgage_workflow_downloader_coverage_automation_repair_not_statement_gap_count')} "
        f"properties={report.get('mortgage_workflow_downloader_coverage_automation_repair_not_statement_gap_properties')}"
    )
if report.get("mortgage_workflow_downloader_coverage_unexpected_configured_downloader_count"):
    alerts.append(
        "mortgage_downloader_coverage_unexpected_configured "
        f"count={report.get('mortgage_workflow_downloader_coverage_unexpected_configured_downloader_count')} "
        f"properties={report.get('mortgage_workflow_downloader_coverage_unexpected_configured_downloader_properties')}"
    )
    unexpected_downloaders = report.get("mortgage_workflow_downloader_coverage_unexpected_configured_downloaders")
    if isinstance(unexpected_downloaders, list):
        for index, item in enumerate(unexpected_downloaders[:3], start=1):
            compact = compact_blocked_downloader_item(item)
            if compact:
                alerts.append(f"mortgage_downloader_coverage_unexpected_configured_{index} {compact}")
if report.get("mortgage_workflow_downloader_coverage_current_month_statement_gap_count"):
    alerts.append(
        "mortgage_downloader_coverage_current_month_statement_gaps "
        f"count={report.get('mortgage_workflow_downloader_coverage_current_month_statement_gap_count')} "
        f"properties={report.get('mortgage_workflow_downloader_coverage_current_month_statement_gap_properties')} "
        f"stale={report.get('mortgage_workflow_downloader_coverage_stale_statement_count')} "
        f"missing={report.get('mortgage_workflow_downloader_coverage_missing_current_month_statement_count')}"
    )
    coverage_gaps = report.get("mortgage_workflow_downloader_coverage_current_month_statement_gaps")
    if isinstance(coverage_gaps, list):
        for index, item in enumerate(coverage_gaps[:5], start=1):
            compact = compact_coverage_statement_gap_item(item)
            if compact:
                alerts.append(f"mortgage_downloader_coverage_current_month_statement_gap_{index} {compact}")
if report.get("mortgage_workflow_downloader_coverage_downloader_target_month_statement_gap_count"):
    alerts.append(
        "mortgage_downloader_coverage_downloader_target_month_statement_gaps "
        f"count={report.get('mortgage_workflow_downloader_coverage_downloader_target_month_statement_gap_count')} "
        f"properties={report.get('mortgage_workflow_downloader_coverage_downloader_target_month_statement_gap_properties')}"
    )
    target_gaps = report.get("mortgage_workflow_downloader_coverage_downloader_target_month_statement_gaps")
    if isinstance(target_gaps, list):
        for index, item in enumerate(target_gaps[:5], start=1):
            compact = compact_coverage_statement_gap_item(item)
            if compact:
                alerts.append(f"mortgage_downloader_coverage_downloader_target_month_statement_gap_{index} {compact}")
if report.get("mortgage_workflow_replayability_status") == "review":
    alerts.append(
        "mortgage_workflow_replayability_review "
        f"reason={report.get('mortgage_workflow_replayability_reason') or 'unknown'} "
        f"har_blocked={report.get('mortgage_workflow_replayability_har_analysis_blocked_count')} "
        f"manual={report.get('mortgage_workflow_replayability_next_action_manual_count')} "
        f"citadel_status={report.get('mortgage_workflow_replayability_citadel_status') or 'unknown'} "
        f"citadel_ready={report.get('mortgage_workflow_replayability_citadel_ready')} "
        f"citadel_blocker={report.get('mortgage_workflow_replayability_citadel_blocker') or 'unknown'} "
        f"citadel_target_month={report.get('mortgage_workflow_replayability_citadel_target_month') or 'unknown'} "
        f"citadel_target_available={report.get('mortgage_workflow_replayability_citadel_target_month_replayable_document_available')}"
    )
if report.get("mortgage_workflow_evidence_refresh_status") == "review":
    alerts.append(
        "mortgage_workflow_evidence_refresh_review "
        f"reason={report.get('mortgage_workflow_evidence_refresh_reason') or 'unknown'} "
        f"ready={report.get('mortgage_workflow_evidence_refresh_analysis_ready_count')} "
        f"blocked={report.get('mortgage_workflow_evidence_refresh_analysis_blocked_count')} "
        f"scaffolds={report.get('mortgage_workflow_evidence_refresh_scaffold_ready_count')} "
        f"handoff_gaps={report.get('mortgage_workflow_evidence_refresh_handoff_file_gap_count')} "
        f"capture_needed={report.get('mortgage_workflow_evidence_refresh_capture_required_before_offline_next_step_count')} "
        f"capture_needed_properties={report.get('mortgage_workflow_evidence_refresh_capture_required_before_offline_next_step_properties')} "
        f"offline_ready={report.get('mortgage_workflow_evidence_refresh_offline_next_step_ready_count')} "
        f"offline_gaps={report.get('mortgage_workflow_evidence_refresh_offline_next_step_gap_count')} "
        f"offline_gap_properties={report.get('mortgage_workflow_evidence_refresh_offline_next_step_gap_properties')}"
    )
if report.get("mortgage_workflow_evidence_refresh_stale_scaffold_cleanup_count"):
    alerts.append(
        "mortgage_workflow_evidence_refresh_stale_scaffold_cleanup "
        f"count={report.get('mortgage_workflow_evidence_refresh_stale_scaffold_cleanup_count')} "
        f"properties={report.get('mortgage_workflow_evidence_refresh_stale_scaffold_cleanup_properties')} "
        f"paths={report.get('mortgage_workflow_evidence_refresh_stale_scaffold_cleanup_paths')}"
    )
if report.get("mortgage_workflow_evidence_advance_status") == "review":
    alerts.append(
        "mortgage_workflow_evidence_advance_review "
        f"reason={report.get('mortgage_workflow_evidence_advance_reason') or 'unknown'} "
        f"ok={report.get('mortgage_workflow_evidence_advance_ok_count')} "
        f"review={report.get('mortgage_workflow_evidence_advance_review_count')} "
        f"safe={report.get('mortgage_workflow_evidence_advance_safe_to_run_automatically')} "
        f"apply_evidence={report.get('mortgage_workflow_evidence_advance_apply_evidence')} "
        f"apply_registry={report.get('mortgage_workflow_evidence_advance_apply_registry')} "
        f"enable_verified_registry_entry={report.get('mortgage_workflow_evidence_advance_enable_verified_registry_entry')}"
    )
advance_results = report.get("mortgage_workflow_evidence_advance_results")
if isinstance(advance_results, list) and advance_results:
    for index, item in enumerate(advance_results[:3], start=1):
        compact = compact_action_plan_item(item)
        if compact:
            alerts.append(f"mortgage_workflow_evidence_advance_result_{index} {compact}")
capture_queue_entries = report.get("mortgage_workflow_capture_queue_entries")
if isinstance(capture_queue_entries, list) and capture_queue_entries:
    alerts.append(
        "mortgage_workflow_capture_queue "
        f"status={report.get('mortgage_workflow_capture_queue_status')} "
        f"reason={report.get('mortgage_workflow_capture_queue_reason') or 'unknown'} "
        f"count={report.get('mortgage_workflow_capture_queue_capture_count') or len(capture_queue_entries)} "
        f"missing_har={report.get('mortgage_workflow_capture_queue_missing_har_count')} "
        f"needs_bodies={report.get('mortgage_workflow_capture_queue_existing_har_needs_bodies_count')} "
        f"needs_target_pdf={report.get('mortgage_workflow_capture_queue_existing_har_needs_target_month_statement_pdf_count')} "
        f"manual={report.get('mortgage_workflow_capture_queue_manual_capture_count')} "
        f"automatic={report.get('mortgage_workflow_capture_queue_automatic_capture_count')} "
        f"tokenomics_blocking={report.get('mortgage_workflow_capture_queue_tokenomics_blocking_count')} "
        f"tokenomics_non_blocking={report.get('mortgage_workflow_capture_queue_tokenomics_non_blocking_count')} "
        f"workbook_blockers={report.get('mortgage_workflow_capture_queue_tokenomics_workbook_write_blocker_count')} "
        f"workbook_blocker_reasons={report.get('mortgage_workflow_capture_queue_tokenomics_workbook_write_blocker_reasons')} "
        f"command_precondition_statuses={report.get('mortgage_workflow_capture_queue_command_precondition_statuses')} "
        f"blocked_by_auth={report.get('mortgage_workflow_capture_queue_blocked_by_auth_properties')} "
        f"missing_workflow_har={report.get('mortgage_workflow_capture_queue_missing_workflow_har_properties')} "
        f"target_month_workflow_har_capture_required={report.get('mortgage_workflow_capture_queue_target_month_workflow_har_capture_required_properties')} "
        f"existing_workflow_har_paths={report.get('mortgage_workflow_capture_queue_existing_workflow_har_paths')} "
        f"duplicate_target_statement_months={report.get('mortgage_workflow_capture_queue_duplicate_target_statement_months')} "
        f"target_recapture_reasons={report.get('mortgage_workflow_capture_queue_target_month_recapture_reasons')} "
        f"target_candidate_pdf_statuses={report.get('mortgage_workflow_capture_queue_target_month_candidate_pdf_payload_statuses')} "
        f"target_candidate_document_ids={report.get('mortgage_workflow_capture_queue_target_month_candidate_document_ids')} "
        f"credential_lookup_statuses={report.get('mortgage_workflow_capture_queue_credential_lookup_statuses')} "
        f"credential_lookup_failure_reasons={report.get('mortgage_workflow_capture_queue_credential_lookup_failure_reasons')} "
        f"credential_lookup_expected_folder_names={report.get('mortgage_workflow_capture_queue_credential_lookup_expected_folder_names')} "
        f"credential_login_hint_mismatch_properties={report.get('mortgage_workflow_capture_queue_credential_login_hint_mismatch_properties')} "
        f"credential_login_hint_mismatch_overridden_properties={report.get('mortgage_workflow_capture_queue_credential_login_hint_mismatch_overridden_properties')} "
        f"credential_lookup_candidate_counts={report.get('mortgage_workflow_capture_queue_credential_lookup_candidate_counts')} "
        f"credential_repair_count={report.get('mortgage_workflow_credential_repair_action_count')} "
        f"credential_repair_properties={report.get('mortgage_workflow_credential_repair_properties')} "
        f"portal_auth_repair_count={report.get('mortgage_workflow_portal_auth_repair_action_count')} "
        f"portal_auth_repair_properties={report.get('mortgage_workflow_portal_auth_repair_properties')} "
        f"live_cdp_handoffs={report.get('mortgage_workflow_capture_queue_live_cdp_downloader_handoff_count')} "
        f"live_cdp_properties={report.get('mortgage_workflow_capture_queue_live_cdp_downloader_properties')} "
        f"safe={report.get('mortgage_workflow_capture_queue_safe_to_run_automatically')}"
    )
    for index, item in enumerate(capture_queue_entries[:5], start=1):
        compact = compact_action_plan_item(item)
        if compact:
            alerts.append(f"mortgage_workflow_capture_queue_{index} {compact}")
    live_cdp_queue_handoffs = report.get("mortgage_workflow_capture_queue_live_cdp_downloader_handoffs")
    if isinstance(live_cdp_queue_handoffs, list) and live_cdp_queue_handoffs:
        for index, item in enumerate(live_cdp_queue_handoffs[:3], start=1):
            if not isinstance(item, dict):
                continue
            alerts.append(
                "mortgage_workflow_capture_queue_live_cdp_handoff_"
                f"{index} property={item.get('property')} "
                f"auth_blocker={item.get('capture_auth_blocker_reason') or item.get('capture_command_blocked_reason')} "
                f"credential_status={item.get('credential_lookup_status')} "
                f"credential_failure={item.get('credential_lookup_failure_reason')} "
                f"credential_candidate_count={item.get('credential_lookup_candidate_count')} "
                f"ready_after_visible_auth={item.get('live_cdp_downloader_ready_after_visible_auth')} "
                f"command={item.get('live_cdp_downloader_command')}"
            )
tokenomics_capture_queue = report.get("mortgage_workflow_coownership_tokenomics_blocking_capture_queue")
if isinstance(tokenomics_capture_queue, list) and tokenomics_capture_queue:
    alerts.append(
        "coownership_tokenomics_blocking_capture_queue "
        f"count={report.get('mortgage_workflow_coownership_tokenomics_blocking_capture_queue_count') or len(tokenomics_capture_queue)} "
        f"properties={report.get('mortgage_workflow_coownership_tokenomics_blocking_capture_queue_properties')}"
    )
    for index, item in enumerate(tokenomics_capture_queue[:5], start=1):
        compact = compact_action_plan_item(item)
        if compact:
            alerts.append(f"coownership_tokenomics_blocking_capture_queue_{index} {compact}")
if (
    (report.get("mortgage_workflow_capture_queue_processor_processed_count") or 0)
    or report.get("mortgage_workflow_capture_queue_processor_status") == "review"
    or (report.get("mortgage_workflow_capture_queue_processor_queue_blocker_count") or 0)
    or (report.get("mortgage_workflow_capture_queue_processor_pending_apply_count") or 0)
):
    alerts.append(
        "mortgage_workflow_capture_queue_processor "
        f"status={report.get('mortgage_workflow_capture_queue_processor_status')} "
        f"reason={report.get('mortgage_workflow_capture_queue_processor_reason') or 'none'} "
        f"processed={report.get('mortgage_workflow_capture_queue_processor_processed_count')} "
        f"applied={report.get('mortgage_workflow_capture_queue_processor_applied_count')} "
        f"dry_run={report.get('mortgage_workflow_capture_queue_processor_dry_run_processed_count')} "
        f"apply={report.get('mortgage_workflow_capture_queue_processor_apply')} "
        f"process_existing_body_recaptures={report.get('mortgage_workflow_capture_queue_processor_process_existing_body_recaptures')} "
        f"enable_verified_registry_entry={report.get('mortgage_workflow_capture_queue_processor_enable_verified_registry_entry')} "
        f"pending_apply={report.get('mortgage_workflow_capture_queue_processor_pending_apply_count')} "
        f"durable={report.get('mortgage_workflow_capture_queue_processor_durable_completion_count')} "
        f"review={report.get('mortgage_workflow_capture_queue_processor_review_count')} "
        f"missing_har={report.get('mortgage_workflow_capture_queue_processor_missing_har_count')} "
        f"invalid_har={report.get('mortgage_workflow_capture_queue_processor_invalid_har_count')} "
        f"auth_blocked_before_capture={report.get('mortgage_workflow_capture_queue_processor_auth_blocked_before_capture_count')} "
        f"auth_blocked_properties={report.get('mortgage_workflow_capture_queue_processor_auth_blocked_before_capture_properties')} "
        f"existing_har_recapture_skipped={report.get('mortgage_workflow_capture_queue_processor_existing_har_recapture_skipped_count')} "
        f"target_pdf_recapture_skipped={report.get('mortgage_workflow_capture_queue_processor_existing_har_target_month_pdf_recapture_skipped_count')} "
        f"body_recapture_skipped={report.get('mortgage_workflow_capture_queue_processor_existing_body_recapture_skipped_count')} "
        f"tokenomics_workbook_blockers={report.get('mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocker_count')} "
        f"tokenomics_queue_blockers={report.get('mortgage_workflow_capture_queue_processor_tokenomics_queue_blocker_count')} "
        f"queue_blockers={report.get('mortgage_workflow_capture_queue_processor_queue_blocker_count')} "
        f"queue_blocker_properties={report.get('mortgage_workflow_capture_queue_processor_queue_blocker_properties')} "
        f"command_precondition_statuses={report.get('mortgage_workflow_capture_queue_processor_command_precondition_statuses')} "
        f"blocked_by_auth={report.get('mortgage_workflow_capture_queue_processor_blocked_by_auth_properties')} "
        f"target_recapture_reasons={report.get('mortgage_workflow_capture_queue_processor_target_month_recapture_reasons')} "
        f"target_candidate_pdf_statuses={report.get('mortgage_workflow_capture_queue_processor_target_month_candidate_pdf_payload_statuses')} "
        f"target_candidate_document_ids={report.get('mortgage_workflow_capture_queue_processor_target_month_candidate_document_ids')} "
        f"credential_lookup_statuses={report.get('mortgage_workflow_capture_queue_processor_credential_lookup_statuses')} "
        f"credential_lookup_failure_reasons={report.get('mortgage_workflow_capture_queue_processor_credential_lookup_failure_reasons')} "
        f"credential_lookup_expected_folder_names={report.get('mortgage_workflow_capture_queue_processor_credential_lookup_expected_folder_names')} "
        f"credential_login_hint_mismatch_properties={report.get('mortgage_workflow_capture_queue_processor_credential_login_hint_mismatch_properties')} "
        f"credential_login_hint_mismatch_overridden_properties={report.get('mortgage_workflow_capture_queue_processor_credential_login_hint_mismatch_overridden_properties')} "
        f"credential_lookup_candidate_counts={report.get('mortgage_workflow_capture_queue_processor_credential_lookup_candidate_counts')} "
        f"portal_auth_repair_count={report.get('mortgage_workflow_capture_queue_processor_portal_auth_repair_action_count')} "
        f"portal_auth_repair_properties={report.get('mortgage_workflow_capture_queue_processor_portal_auth_repair_properties')} "
        f"live_cdp_handoffs={report.get('mortgage_workflow_capture_queue_processor_live_cdp_downloader_handoff_count')} "
        f"live_cdp_properties={report.get('mortgage_workflow_capture_queue_processor_live_cdp_downloader_properties')} "
        f"missing_workflow_har={report.get('mortgage_workflow_capture_queue_processor_missing_workflow_har_properties')} "
        f"markdown={report.get('mortgage_workflow_capture_queue_processor_markdown')}"
    )
    live_cdp_handoffs = report.get("mortgage_workflow_capture_queue_processor_live_cdp_downloader_handoffs")
    if isinstance(live_cdp_handoffs, list) and live_cdp_handoffs:
        for index, item in enumerate(live_cdp_handoffs[:3], start=1):
            if not isinstance(item, dict):
                continue
            alerts.append(
                "mortgage_workflow_live_cdp_downloader_handoff_"
                f"{index} property={item.get('property')} "
                f"next_action={item.get('capture_auth_next_action') or item.get('next_action')} "
                f"ready_after_visible_auth={item.get('live_cdp_downloader_ready_after_visible_auth')} "
                f"command={item.get('live_cdp_downloader_command')}"
            )
    recapture_requirements = report.get("mortgage_workflow_capture_queue_processor_existing_body_recapture_requirements")
    if isinstance(recapture_requirements, list) and recapture_requirements:
        for index, item in enumerate(recapture_requirements[:3], start=1):
            if not isinstance(item, dict):
                continue
            alerts.append(
                "mortgage_workflow_body_recapture_requirement_"
                f"{index} property={item.get('property')} "
                f"paths={item.get('missing_response_body_paths')} "
                f"validation={item.get('validation_command')}"
            )
    target_pdf_requirements = report.get(
        "mortgage_workflow_capture_queue_processor_existing_har_target_month_pdf_recapture_requirements"
    )
    if isinstance(target_pdf_requirements, list) and target_pdf_requirements:
        for index, item in enumerate(target_pdf_requirements[:3], start=1):
            if not isinstance(item, dict):
                continue
            alerts.append(
                "mortgage_workflow_target_pdf_recapture_requirement_"
                f"{index} property={item.get('property')} "
                f"required_capture_quality={item.get('required_capture_quality')} "
                f"paths={item.get('missing_response_body_paths')} "
                f"expected_document_ids={item.get('expected_document_ids')} "
                f"body_recapture_capture_method={item.get('body_recapture_capture_method')} "
                f"validation={item.get('validation_command')}"
            )
property_matrix = report.get("mortgage_workflow_property_matrix")
if isinstance(property_matrix, list) and property_matrix:
    alerts.append(
        "mortgage_workflow_property_matrix "
        f"count={report.get('mortgage_workflow_property_matrix_count') or len(property_matrix)} "
        f"review={report.get('mortgage_workflow_property_matrix_review_count')} "
        f"manual={report.get('mortgage_workflow_property_matrix_manual_action_count')} "
        f"safe={report.get('mortgage_workflow_property_matrix_safe_action_count')}"
    )
    for index, item in enumerate(property_matrix[:3], start=1):
        compact = compact_property_matrix_item(item)
        if compact:
            alerts.append(f"mortgage_workflow_property_matrix_{index} {compact}")
blocked_with_current = report.get("mortgage_workflow_automation_blocked_with_current_statement_records")
if isinstance(blocked_with_current, list) and blocked_with_current:
    alerts.append(
        "mortgage_workflow_automation_blocked_with_current_statement "
        f"count={report.get('mortgage_workflow_automation_blocked_with_current_statement_count') or len(blocked_with_current)} "
        f"properties={report.get('mortgage_workflow_automation_blocked_with_current_statement_properties')}"
    )
    for index, item in enumerate(blocked_with_current[:5], start=1):
        compact = compact_property_matrix_item(item)
        if compact:
            alerts.append(f"mortgage_workflow_automation_blocked_with_current_statement_{index} {compact}")
future_repairs = report.get("mortgage_workflow_future_automation_repair_actions")
if isinstance(future_repairs, list) and future_repairs:
    alerts.append(
        "mortgage_workflow_future_automation_repair "
        f"count={report.get('mortgage_workflow_future_automation_repair_action_count') or len(future_repairs)} "
        f"properties={report.get('mortgage_workflow_future_automation_repair_properties')} "
        f"reasons={report.get('mortgage_workflow_future_automation_repair_reasons')}"
    )
    for index, item in enumerate(future_repairs[:5], start=1):
        compact = compact_future_automation_repair_item(item)
        if compact:
            alerts.append(f"mortgage_workflow_future_automation_repair_{index} {compact}")
workbook_blockers = report.get("mortgage_workflow_coownership_tokenomics_workbook_write_blockers")
if report.get("mortgage_workflow_tokenomics_workbook_partial_ready_property_write_applied"):
    alerts.append(
        "coownership_tokenomics_workbook_partial_ready_property_write "
        f"properties={report.get('mortgage_workflow_tokenomics_workbook_partial_ready_property_write_properties')} "
        f"cleared_stale={report.get('mortgage_workflow_tokenomics_workbook_partial_ready_property_cleared_copied_stale_properties')}"
    )
if isinstance(workbook_blockers, list) and workbook_blockers:
    alerts.append(
        "coownership_tokenomics_workbook_write_blocked "
        f"count={report.get('mortgage_workflow_coownership_tokenomics_workbook_write_blocker_count') or len(workbook_blockers)} "
        f"properties={report.get('mortgage_workflow_coownership_tokenomics_workbook_write_blocker_properties')} "
        f"reasons={report.get('mortgage_workflow_coownership_tokenomics_workbook_write_blocker_reasons')}"
    )
    for index, item in enumerate(workbook_blockers[:5], start=1):
        compact = compact_workbook_blocker_item(item)
        if compact:
            alerts.append(f"coownership_tokenomics_workbook_write_blocker_{index} {compact}")
registry_handoffs = report.get("mortgage_workflow_registry_handoffs")
if isinstance(registry_handoffs, list) and registry_handoffs:
    alerts.append(
        "mortgage_workflow_registry_handoff "
        f"count={report.get('mortgage_workflow_registry_handoff_count') or len(registry_handoffs)} "
        f"safe_register={report.get('mortgage_workflow_registry_safe_to_register_automatically_count')}"
    )
    for index, item in enumerate(registry_handoffs[:3], start=1):
        compact = compact_action_plan_item(item)
        if compact:
            alerts.append(f"mortgage_workflow_registry_handoff_{index} {compact}")
next_action_plan = report.get("mortgage_workflow_downloader_coverage_next_action_plan")
if isinstance(next_action_plan, list) and next_action_plan:
    alerts.append(
        "mortgage_downloader_coverage_next_action_plan "
        f"count={report.get('mortgage_workflow_downloader_coverage_next_action_plan_count') or len(next_action_plan)} "
        f"manual={report.get('mortgage_workflow_downloader_coverage_next_action_manual_count')} "
        f"safe={report.get('mortgage_workflow_downloader_coverage_next_action_safe_to_run_automatically_count')}"
    )
    for index, item in enumerate(next_action_plan[:3], start=1):
        compact = compact_action_plan_item(item)
        if compact:
            alerts.append(f"mortgage_downloader_coverage_next_action_{index} {compact}")
replayability_plan = report.get("mortgage_workflow_replayability_next_action_plan")
if isinstance(replayability_plan, list) and replayability_plan:
    alerts.append(
        "mortgage_workflow_replayability_next_action_plan "
        f"count={report.get('mortgage_workflow_replayability_next_action_plan_count') or len(replayability_plan)} "
        f"manual={report.get('mortgage_workflow_replayability_next_action_manual_count')} "
        f"auto_count={report.get('mortgage_workflow_replayability_next_action_safe_to_run_automatically_count')} "
        f"credential_repair_count={intish(report.get('mortgage_workflow_replayability_credential_repair_action_count'))} "
        f"credential_repair_properties={compact_list(report.get('mortgage_workflow_replayability_credential_repair_properties') or [])} "
        f"portal_auth_repair_count={intish(report.get('mortgage_workflow_replayability_portal_auth_repair_action_count'))} "
        f"portal_auth_repair_properties={compact_list(report.get('mortgage_workflow_replayability_portal_auth_repair_properties') or [])} "
        f"safe={report.get('mortgage_workflow_replayability_safe_to_run_automatically')}"
    )
    for index, item in enumerate(replayability_plan[:3], start=1):
        compact = compact_action_plan_item(item)
        if compact:
            alerts.append(f"mortgage_workflow_replayability_next_action_{index} {compact}")
evidence_refresh_plan = report.get("mortgage_workflow_evidence_refresh_next_action_plan")
if isinstance(evidence_refresh_plan, list) and evidence_refresh_plan:
    alerts.append(
        "mortgage_workflow_evidence_refresh_next_action_plan "
        f"count={report.get('mortgage_workflow_evidence_refresh_next_action_plan_count') or len(evidence_refresh_plan)} "
        f"manual={report.get('mortgage_workflow_evidence_refresh_next_action_manual_count')} "
        f"auto_count={report.get('mortgage_workflow_evidence_refresh_next_action_safe_to_run_automatically_count')} "
        f"safe={report.get('mortgage_workflow_evidence_refresh_safe_to_run_automatically')} "
        f"ready={report.get('mortgage_workflow_evidence_refresh_analysis_ready_count')} "
        f"blocked={report.get('mortgage_workflow_evidence_refresh_analysis_blocked_count')}"
    )
    for index, item in enumerate(evidence_refresh_plan[:3], start=1):
        compact = compact_action_plan_item(item)
        if compact:
            alerts.append(f"mortgage_workflow_evidence_refresh_next_action_{index} {compact}")
if report.get("mortgage_downloader_citadel_auth_retryable_portal_failure"):
    alerts.append(
        "citadel_retryable_portal_failure "
        f"reason={report.get('mortgage_downloader_citadel_auth_retryable_reason') or 'unknown'} "
        f"direct_status={report.get('mortgage_downloader_citadel_direct_auth_status') or 'unknown'} "
        f"credential_verified={report.get('mortgage_downloader_citadel_prepare_password_matched_before_update') or report.get('mortgage_downloader_citadel_prepare_password_updated')}"
    )
elif report.get("mortgage_downloader_citadel_credential_state_drift_suspected"):
    alerts.append(
        "citadel_credential_state_drift_suspected "
        f"portal={report.get('mortgage_downloader_citadel_manual_auth_portal_url') or 'unknown'} "
        f"direct_status={report.get('mortgage_downloader_citadel_direct_auth_status') or 'unknown'} "
        f"no_mfa_handoff={report.get('mortgage_downloader_citadel_direct_no_mfa_handoff_after_password')}"
    )
if report.get("mortgage_downloader_citadel_otp_required"):
    alerts.append(
        "citadel_otp_required "
        f"send_via={report.get('mortgage_downloader_citadel_direct_otp_send_via_type') or 'unknown'} "
        f"otp_file={report.get('mortgage_downloader_citadel_otp_file') or 'unknown'} "
        f"next_command={report.get('mortgage_downloader_citadel_otp_next_command') or 'unknown'}"
    )
if report.get("mortgage_downloader_citadel_direct_no_mfa_handoff_after_password"):
    alerts.append(
        "citadel_no_mfa_handoff_after_password "
        f"reason={report.get('mortgage_downloader_citadel_direct_no_mfa_handoff_reason') or 'unknown'} "
        f"direct_status={report.get('mortgage_downloader_citadel_direct_auth_status') or 'unknown'}"
    )
if (
    report.get("mortgage_downloader_citadel_auth_preflight_status") == "review"
    and report.get("mortgage_downloader_citadel_auth_preflight_authenticated_found") is False
):
    alerts.append(
        "citadel_auth_preflight_no_authenticated_tab "
        f"reason={report.get('mortgage_downloader_citadel_auth_preflight_reason') or 'unknown'} "
        f"scanned={report.get('mortgage_downloader_citadel_auth_preflight_scanned_count')} "
        f"candidates={report.get('mortgage_downloader_citadel_auth_preflight_candidate_count')} "
        f"login_tabs={report.get('mortgage_downloader_citadel_auth_preflight_login_tab_count')}"
    )
if report.get("mortgage_downloader_citadel_har_workflow_replay_blocker"):
    requirement_summary = compact_response_body_requirements(
        report.get("mortgage_downloader_citadel_har_workflow_response_body_requirements")
    )
    alerts.append(
        "citadel_har_replay_blocked "
        f"blocker={report.get('mortgage_downloader_citadel_har_workflow_replay_blocker')} "
        f"quality={report.get('mortgage_downloader_citadel_har_workflow_capture_quality_status') or 'unknown'} "
        f"target_month={report.get('mortgage_downloader_citadel_har_workflow_target_month') or 'unknown'} "
        f"target_available={report.get('mortgage_downloader_citadel_har_workflow_target_month_replayable_document_available')} "
        f"statement_months={report.get('mortgage_downloader_citadel_har_workflow_replayable_statement_months') or report.get('mortgage_downloader_citadel_har_workflow_statement_document_months') or []} "
        f"target_pdf_filenames={report.get('mortgage_downloader_citadel_har_workflow_target_month_direct_pdf_filenames') or []} "
        f"target_pdf_body_missing={report.get('mortgage_downloader_citadel_har_workflow_target_month_direct_pdf_body_missing_candidate_count')} "
        f"missing_bodies={report.get('mortgage_downloader_citadel_har_workflow_missing_response_body_count')} "
        f"direct_pdfs={report.get('mortgage_downloader_citadel_har_workflow_direct_pdf_response_count')} "
        f"source_direct_pdf_candidates={report.get('mortgage_downloader_citadel_har_workflow_source_direct_pdf_candidate_count')} "
        f"direct_pdf_missing_bodies={report.get('mortgage_downloader_citadel_har_workflow_direct_pdf_missing_response_count')} "
        f"response_requirements={requirement_summary or 'none'}"
    )
if (
    report.get("mortgage_downloader_citadel_har_preflight_status") == "review"
    or report.get("mortgage_downloader_citadel_har_preflight_can_replay_documents") is False
):
    requirement_summary = compact_response_body_requirements(
        report.get("mortgage_downloader_citadel_har_preflight_response_body_requirements")
    )
    alerts.append(
        "citadel_har_preflight_not_replayable "
        f"blocker={report.get('mortgage_downloader_citadel_har_preflight_replay_blocker') or 'unknown'} "
        f"quality={report.get('mortgage_downloader_citadel_har_preflight_capture_quality_status') or 'unknown'} "
        f"target_month={report.get('mortgage_downloader_citadel_har_preflight_target_month') or 'unknown'} "
        f"target_available={report.get('mortgage_downloader_citadel_har_preflight_target_month_replayable_document_available')} "
        f"statement_months={report.get('mortgage_downloader_citadel_har_preflight_replayable_statement_months') or report.get('mortgage_downloader_citadel_har_preflight_statement_document_months') or []} "
        f"target_pdf_filenames={report.get('mortgage_downloader_citadel_har_preflight_target_month_direct_pdf_filenames') or []} "
        f"target_pdf_body_missing={report.get('mortgage_downloader_citadel_har_preflight_target_month_direct_pdf_body_missing_candidate_count')} "
        f"missing_bodies={report.get('mortgage_downloader_citadel_har_preflight_missing_response_body_count')} "
        f"direct_pdfs={report.get('mortgage_downloader_citadel_har_preflight_direct_pdf_response_count')} "
        f"source_direct_pdf_candidates={report.get('mortgage_downloader_citadel_har_preflight_source_direct_pdf_candidate_count')} "
        f"direct_pdf_missing_bodies={report.get('mortgage_downloader_citadel_har_preflight_direct_pdf_missing_response_count')} "
        f"next_action={report.get('mortgage_downloader_citadel_har_preflight_next_action_status') or 'unknown'} "
        f"next_command={report.get('mortgage_downloader_citadel_har_preflight_next_action_command') or 'unknown'} "
        f"capture_command={report.get('mortgage_downloader_citadel_har_preflight_next_action_capture_command') or 'unknown'} "
        f"required_paths={report.get('mortgage_downloader_citadel_har_preflight_next_action_required_response_paths')} "
        f"response_requirements={requirement_summary or 'none'}"
    )
if report.get("mortgage_downloader_citadel_capture_plan_status"):
    alerts.append(
        "citadel_capture_plan "
        f"status={report.get('mortgage_downloader_citadel_capture_plan_status')} "
        f"latest_live_auth_status={report.get('mortgage_downloader_citadel_capture_plan_latest_live_auth_status') or 'unknown'} "
        f"latest_live_auth_blocker={report.get('mortgage_downloader_citadel_capture_plan_latest_live_auth_blocker') or 'unknown'} "
        f"latest_live_auth_visible_reason={report.get('mortgage_downloader_citadel_capture_plan_latest_live_auth_auth_failure_visible_reason') or 'unknown'} "
        f"capture_precondition_status={report.get('mortgage_downloader_citadel_capture_plan_capture_precondition_status') or 'unknown'} "
        f"capture_precondition_blocker={report.get('mortgage_downloader_citadel_capture_plan_capture_precondition_blocker') or 'unknown'} "
        f"capture_precondition_safe_to_capture_now={report.get('mortgage_downloader_citadel_capture_plan_capture_precondition_safe_to_capture_now')} "
        f"capture_precondition_prepare_command={report.get('mortgage_downloader_citadel_capture_plan_capture_precondition_prepare_command') or 'unknown'} "
        f"capture_precondition_safe_to_prepare_now={report.get('mortgage_downloader_citadel_capture_plan_capture_precondition_safe_to_prepare_now')} "
        f"capture_required={report.get('mortgage_downloader_citadel_capture_plan_capture_required')} "
        f"capture_command={report.get('mortgage_downloader_citadel_capture_plan_capture_command') or 'unknown'} "
        f"next_command={report.get('mortgage_downloader_citadel_capture_plan_next_command') or 'unknown'} "
        f"capture_har={report.get('mortgage_downloader_citadel_capture_plan_capture_har_path') or 'unknown'} "
        f"post_capture_diagnostics={report.get('mortgage_downloader_citadel_capture_plan_post_capture_diagnostics_command') or 'unknown'} "
        f"post_capture_check={report.get('mortgage_downloader_citadel_capture_plan_post_capture_check_command') or 'unknown'} "
        f"post_capture_check_script={report.get('mortgage_downloader_citadel_capture_plan_post_capture_check_script_path') or 'unknown'} "
        f"validate_existing_capture={report.get('mortgage_downloader_citadel_capture_plan_validate_existing_capture_command') or 'unknown'} "
        f"advance_capture_workflow_evidence_dry_run={report.get('mortgage_downloader_citadel_capture_plan_advance_capture_workflow_evidence_dry_run_command') or 'unknown'} "
        f"advance_capture_workflow_evidence_apply={report.get('mortgage_downloader_citadel_capture_plan_advance_capture_workflow_evidence_apply_command') or 'unknown'} "
        f"advance_workflow_evidence={report.get('mortgage_downloader_citadel_capture_plan_advance_workflow_evidence_command') or 'unknown'} "
        f"advance_workflow_evidence_apply={report.get('mortgage_downloader_citadel_capture_plan_advance_workflow_evidence_apply_command') or 'unknown'} "
        f"target_month={report.get('mortgage_downloader_citadel_capture_plan_target_month') or 'unknown'} "
        f"target_available={report.get('mortgage_downloader_citadel_capture_plan_target_month_replayable_document_available')} "
        f"target_pdf_filenames={report.get('mortgage_downloader_citadel_capture_plan_target_month_direct_pdf_filenames') or []} "
        f"target_pdf_body_missing={report.get('mortgage_downloader_citadel_capture_plan_target_month_direct_pdf_body_missing_candidate_count')} "
        f"checklist={report.get('mortgage_downloader_citadel_capture_plan_markdown') or 'unknown'} "
        f"source_direct_pdf_candidates={report.get('mortgage_downloader_citadel_capture_plan_source_direct_pdf_candidate_count')} "
        f"source_required_response_candidates={report.get('mortgage_downloader_citadel_capture_plan_source_required_response_candidate_count')} "
        f"raw_source_urls_included={report.get('mortgage_downloader_citadel_capture_plan_raw_source_urls_included')}"
    )
if report.get("mortgage_downloader_citadel_capture_status") in {"auth_required", "cdp_unavailable"}:
    alerts.append(
        "citadel_capture_not_ready "
        f"status={report.get('mortgage_downloader_citadel_capture_status')} "
        f"reason={report.get('mortgage_downloader_citadel_capture_reason') or 'unknown'} "
        f"authenticated={report.get('mortgage_downloader_citadel_capture_authenticated_found')} "
        f"login_tabs={report.get('mortgage_downloader_citadel_capture_login_tab_count')} "
        f"portal={report.get('mortgage_downloader_citadel_capture_manual_auth_portal_url') or 'unknown'}"
    )
if report.get("mortgage_downloader_citadel_capture_missing_required_response_paths"):
    alerts.append(
        "citadel_capture_missing_required_response_bodies "
        f"missing_paths={report.get('mortgage_downloader_citadel_capture_missing_required_response_paths')} "
        f"missing_counts={report.get('mortgage_downloader_citadel_capture_missing_required_response_path_counts')} "
        f"source_required_response_candidates={report.get('mortgage_downloader_citadel_capture_source_required_response_candidate_count')} "
        f"captured_paths={report.get('mortgage_downloader_citadel_capture_captured_required_response_paths')}"
    )
if (
    report.get("mortgage_downloader_citadel_capture_target_month_replayable_document_available") is False
    or (
        report.get("mortgage_downloader_citadel_capture_target_month_replayable_document_payload_count") == 0
        and report.get("mortgage_downloader_citadel_capture_target_month")
    )
):
    alerts.append(
        "citadel_capture_target_month_not_replayable "
        f"target_month={report.get('mortgage_downloader_citadel_capture_target_month')} "
        f"target_available={report.get('mortgage_downloader_citadel_capture_target_month_replayable_document_available')} "
        f"target_payloads={report.get('mortgage_downloader_citadel_capture_target_month_replayable_document_payload_count')} "
        f"statement_months={report.get('mortgage_downloader_citadel_capture_replayable_statement_months') or report.get('mortgage_downloader_citadel_capture_statement_document_months') or []}"
    )
if (
    not report.get("mortgage_downloader_citadel_direct_no_mfa_handoff_after_password")
    and
    not report.get("mortgage_downloader_citadel_credential_state_drift_suspected")
    and report.get("mortgage_downloader_citadel_manual_auth_required")
):
    alerts.append(
        "citadel_manual_auth_required "
        f"reason={report.get('mortgage_downloader_citadel_manual_auth_reason') or 'unknown'} "
        f"portal={report.get('mortgage_downloader_citadel_manual_auth_portal_url') or 'unknown'}"
    )
if report.get("status") in {"skipped_not_friday", "already_done_for_week"}:
    alerts = []
report["alert_count"] = len(alerts)
report["alerts"] = alerts
alert_file.write_text(("\n".join(alerts) + "\n") if alerts else "", encoding="utf-8")
tmp = report_file.with_suffix(report_file.suffix + ".tmp")
tmp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(report_file)
with history_file.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(report, sort_keys=True) + "\n")
print(json.dumps(report, indent=2, sort_keys=True))
PY
    }

if [ "$SCOPE_GUARD_ERROR" = "true" ]; then
  write_report "$STATUS" "$SCOPE_GUARD_RC" "$REASON"
  echo "[baselane-weekly] refusing invalid financial source path for Baselane automation; see $SCOPE_GUARD_FILE" >&2
  exit "$SCOPE_GUARD_RC"
fi

if [ -f "$STATE_FILE" ]; then
  LAST_WEEK="$(cat "$STATE_FILE" 2>/dev/null || true)"
fi

if [ "$FORCE_WEEKLY" != "1" ] && [ "$DOW" != "5" ]; then
  STATUS="skipped_not_friday"
  REASON="not_friday"
  DETERMINISTIC_VERIFICATION_IDEMPOTENT=true
  DETERMINISTIC_VERIFICATION_IDEMPOTENT_REASON="non_friday_noop_no_outputs_mutated"
  write_report "$STATUS" 0 "$REASON"
  exit 0
fi

if [ "$FORCE_WEEKLY" != "1" ] && [ "$LAST_WEEK" = "$ISO_WEEK" ]; then
  STATUS="already_done_for_week"
  REASON="state_file_matches_iso_week"
  DETERMINISTIC_VERIFICATION_IDEMPOTENT=true
  DETERMINISTIC_VERIFICATION_IDEMPOTENT_REASON="already_done_noop_no_outputs_mutated"
  write_report "$STATUS" 0 "$REASON"
  exit 0
fi

if [ ! -x "$HOOK" ]; then
  STATUS="failed_missing_hook"
  REASON="missing_or_not_executable_hook"
  DETERMINISTIC_VERIFICATION_IDEMPOTENT_REASON="missing_hook"
  write_report "$STATUS" 1 "$REASON"
  exit 1
fi

if [ -x "$ROOT/scripts/baselane_disk_space_preflight.py" ]; then
  set +e
  timeout --kill-after="${BASELANE_DISK_PREFLIGHT_KILL_AFTER_SECONDS}s" \
    "${BASELANE_DISK_PREFLIGHT_TIMEOUT_SECONDS}s" \
    "$PY" "$ROOT/scripts/baselane_disk_space_preflight.py" \
    --path "dropbox_root=$DROPBOX_ROOT" \
    --path "baselane_ledger_dir=$BASELANE_LEDGER_DIR" \
    --min-free-mib "$BASELANE_DISK_PREFLIGHT_MIN_FREE_MIB" \
    --path-timeout-seconds "$BASELANE_DISK_PREFLIGHT_PATH_TIMEOUT_SECONDS" \
    --report "$DISK_PREFLIGHT_REPORT_FILE" >/dev/null
  DISK_PREFLIGHT_RC="$?"
  set -e
  if [ "$DISK_PREFLIGHT_RC" -eq 0 ]; then
    DISK_PREFLIGHT_STATUS="ok"
  else
    DISK_PREFLIGHT_STATUS="review"
    STATUS="review"
    REASON="disk_space_preflight_review"
    DETERMINISTIC_VERIFICATION_IDEMPOTENT=false
    DETERMINISTIC_VERIFICATION_IDEMPOTENT_REASON="low_disk_no_outputs_mutated"
    write_report "$STATUS" 0 "$REASON"
    echo "[baselane-weekly] holding weekly file updates because local disk preflight is review; see $DISK_PREFLIGHT_REPORT_FILE" >&2
    exit 0
  fi
else
  DISK_PREFLIGHT_STATUS="missing"
fi

CURRENT_STEP="stale_financial_artifact_guard"
STALE_FINANCIAL_ARTIFACT_GUARD_SCRIPT="$ROOT/scripts/baselane_stale_financial_artifact_guard.py"
if [ ! -f "$STALE_FINANCIAL_ARTIFACT_GUARD_SCRIPT" ]; then
  STALE_FINANCIAL_ARTIFACT_GUARD_STATUS="failed_missing_script"
  STATUS="failed"
  REASON="stale_financial_artifact_guard_missing"
  write_report "$STATUS" 1 "$REASON"
  echo "[baselane-weekly] missing stale financial artifact guard: $STALE_FINANCIAL_ARTIFACT_GUARD_SCRIPT" >&2
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
  STATUS="review"
  REASON="stale_financial_artifacts"
  DETERMINISTIC_VERIFICATION_IDEMPOTENT=false
  DETERMINISTIC_VERIFICATION_IDEMPOTENT_REASON="stale_financial_artifacts_no_outputs_mutated"
  write_report "$STATUS" 0 "$REASON"
  echo "[baselane-weekly] holding weekly file updates because stale financial artifacts remain; see $STALE_FINANCIAL_ARTIFACT_GUARD_FILE" >&2
  exit 0
else
  STALE_FINANCIAL_ARTIFACT_GUARD_STATUS="failed"
  STATUS="failed"
  REASON="stale_financial_artifact_guard_failed"
  write_report "$STATUS" "$stale_financial_artifact_guard_rc" "$REASON"
  echo "[baselane-weekly] stale financial artifact guard failed (rc=$stale_financial_artifact_guard_rc)" >&2
  exit "$stale_financial_artifact_guard_rc"
fi

set +e
    "$HOOK"
    RETURN_CODE="$?"
    set -e

    if [ "$RETURN_CODE" -eq 0 ]; then
      DETERMINISTIC_VERIFICATION_IDEMPOTENT=true
      DETERMINISTIC_VERIFICATION_IDEMPOTENT_REASON="weekly_hook_completed"
      set +e
  "$PY" "$ROOT/scripts/baselane_ecogl_data_quality_autonomy.py" \
    --root "$ROOT" \
    --report "$ROOT/reports/baselane_ecogl_data_quality_autonomy.json" \
    --safe-csv "$ROOT/reports/baselane_ecogl_auto_safe_actions.csv" \
    --exceptions-csv "$ROOT/reports/baselane_ecogl_data_quality_exceptions.csv" \
    --markdown "$ROOT/reports/baselane_ecogl_data_quality_autonomy.md"
  ecogl_autonomy_rc="$?"
  set -e
  if [ "${BASELANE_HEMLANE_AUTO_TAG_ENABLED:-1}" = "1" ] && [ -f "$ROOT/scripts/baselane_hemlane_live_transactions.py" ]; then
    set +e
    "$PY" "$ROOT/scripts/baselane_hemlane_live_transactions.py" \
      --root "$ROOT" \
      --report "$ROOT/reports/hemlane_live_transactions.json" >/dev/null
    hemlane_live_transactions_rc="$?"
    set -e
    if [ "$hemlane_live_transactions_rc" -ne 0 ]; then
      echo "[baselane-weekly] Hemlane live transaction evidence unavailable rc=$hemlane_live_transactions_rc; source-fix evidence will fail closed" >&2
    fi
  fi
  set +e
  "$PY" "$ROOT/scripts/baselane_ecogl_source_fix_plan.py" \
    --root "$ROOT" \
    --report "$ROOT/reports/baselane_ecogl_source_fix_plan.json" \
    --actions-csv "$ROOT/reports/baselane_ecogl_source_fix_actions.csv" \
    --markdown "$ROOT/reports/baselane_ecogl_source_fix_plan.md"
  ecogl_source_fix_rc="$?"
  set -e
  set +e
  timeout --kill-after=15s "${ECOGL_SOURCE_FIX_EVIDENCE_TIMEOUT_SECONDS}s" "$PY" "$ROOT/scripts/baselane_ecogl_source_fix_evidence.py" \
    --root "$ROOT" \
    --actions-csv "$ROOT/reports/baselane_ecogl_source_fix_actions.csv" \
    --source-plan "$ROOT/reports/baselane_ecogl_source_fix_plan.json" \
    --report "$ROOT/reports/baselane_ecogl_source_fix_evidence.json" \
    --markdown "$ROOT/reports/baselane_ecogl_source_fix_evidence.md"
  ecogl_source_fix_evidence_rc="$?"
  set -e
  set +e
  "$PY" "$ROOT/scripts/baselane_ecogl_source_fix_verifier.py" \
    --root "$ROOT" \
    --actions-csv "$ROOT/reports/baselane_ecogl_source_fix_actions.csv" \
    --report "$ROOT/reports/baselane_ecogl_source_fix_verifier.json" \
    --markdown "$ROOT/reports/baselane_ecogl_source_fix_verifier.md"
  ecogl_source_fix_verifier_rc="$?"
  set -e
  set +e
  "$PY" "$ROOT/scripts/baselane_ecogl_source_fix_corrections.py" \
    --root "$ROOT" \
    --evidence "$ROOT/reports/baselane_ecogl_source_fix_evidence.json" \
    --verifier "$ROOT/reports/baselane_ecogl_source_fix_verifier.json" \
    --report "$ROOT/reports/baselane_ecogl_source_fix_corrections.json" \
    --csv "$ROOT/reports/baselane_ecogl_source_fix_corrections.csv" \
    --markdown "$ROOT/reports/baselane_ecogl_source_fix_corrections.md"
  ecogl_source_fix_corrections_rc="$?"
  set -e
  if [ -f "$ROOT/scripts/baselane_ecogl_source_fix_approval.py" ]; then
    set +e
    "$PY" "$ROOT/scripts/baselane_ecogl_source_fix_approval.py" \
      --root "$ROOT" \
      --corrections-report "$ROOT/reports/baselane_ecogl_source_fix_corrections.json" \
      --corrections-csv "$ROOT/reports/baselane_ecogl_source_fix_corrections.csv" \
      --approval "$ROOT/reports/baselane_ecogl_source_fix_approval.json" \
      --approved-csv "$ROOT/reports/baselane_ecogl_source_fix_approved_corrections.csv" \
      --markdown "$ROOT/reports/baselane_ecogl_source_fix_approval.md"
    ecogl_source_fix_approval_rc="$?"
    set -e
  else
    ecogl_source_fix_approval_rc=0
  fi
  if [ -f "$ROOT/scripts/baselane_ecogl_source_fix_correction_validator.py" ]; then
    set +e
    "$PY" "$ROOT/scripts/baselane_ecogl_source_fix_correction_validator.py" \
      --root "$ROOT" \
      --corrections-csv "$ROOT/reports/baselane_ecogl_source_fix_approved_corrections.csv" \
      --corrections-report "$ROOT/reports/baselane_ecogl_source_fix_approval.json" \
      --report "$ROOT/reports/baselane_ecogl_source_fix_correction_validation.json" \
      --csv "$ROOT/reports/baselane_ecogl_source_fix_correction_validation.csv" \
      --markdown "$ROOT/reports/baselane_ecogl_source_fix_correction_validation.md"
    ecogl_source_fix_correction_validation_rc="$?"
    set -e
  else
    ecogl_source_fix_correction_validation_rc=0
  fi
  if [ -f "$ROOT/scripts/baselane_ecogl_source_fix_apply_plan.py" ]; then
    set +e
    "$PY" "$ROOT/scripts/baselane_ecogl_source_fix_apply_plan.py" \
      --root "$ROOT" \
      --approved-csv "$ROOT/reports/baselane_ecogl_source_fix_approved_corrections.csv" \
      --validation-report "$ROOT/reports/baselane_ecogl_source_fix_correction_validation.json" \
      --report "$ROOT/reports/baselane_ecogl_source_fix_apply_plan.json" \
      --csv "$ROOT/reports/baselane_ecogl_source_fix_apply_plan.csv" \
      --markdown "$ROOT/reports/baselane_ecogl_source_fix_apply_plan.md"
    ecogl_source_fix_apply_plan_rc="$?"
    set -e
  else
    ecogl_source_fix_apply_plan_rc=0
  fi
  if [ -f "$ROOT/scripts/baselane_ecogl_source_fix_apply.py" ]; then
    set +e
    "$PY" "$ROOT/scripts/baselane_ecogl_source_fix_apply.py" \
      --root "$ROOT" \
      --apply-plan-csv "$ROOT/reports/baselane_ecogl_source_fix_apply_plan.csv" \
      --source-index-csv "$ROOT/reports/baselane_source_transaction_index.csv" \
      --report "$ROOT/reports/baselane_ecogl_source_fix_apply.json" \
      --csv "$ROOT/reports/baselane_ecogl_source_fix_apply.csv" \
      --markdown "$ROOT/reports/baselane_ecogl_source_fix_apply.md" \
      --payload "$ROOT/reports/baselane_ecogl_source_fix_apply_payload.json"
    ecogl_source_fix_apply_rc="$?"
    set -e
  else
    ecogl_source_fix_apply_rc=0
  fi
  if [ -f "$ROOT/scripts/baselane_ecogl_source_fix_action_queue.py" ]; then
    set +e
    "$PY" "$ROOT/scripts/baselane_ecogl_source_fix_action_queue.py" \
      --root "$ROOT" \
      --report "$ROOT/reports/baselane_ecogl_source_fix_action_queue.json" \
      --csv "$ROOT/reports/baselane_ecogl_source_fix_action_queue.csv" \
      --markdown "$ROOT/reports/baselane_ecogl_source_fix_action_queue.md"
    ecogl_source_fix_action_queue_rc="$?"
    set -e
  else
    ecogl_source_fix_action_queue_rc=0
  fi
  if [ -f "$ROOT/scripts/baselane_native_split_plan.py" ]; then
    set +e
    "$PY" "$ROOT/scripts/baselane_native_split_plan.py" \
      --source-index "$ROOT/reports/baselane_source_transaction_index.csv" \
      --report "$ROOT/reports/baselane_native_split_plan.json" \
      --csv "$ROOT/reports/baselane_native_split_plan.csv" \
      --markdown "$ROOT/reports/baselane_native_split_plan.md"
    native_split_plan_rc="$?"
    set -e
  else
    native_split_plan_rc=0
  fi
  if [ -f "$ROOT/scripts/baselane_apply_native_splits.py" ]; then
    set +e
    BASELANE_NATIVE_SPLIT_APPLY="${BASELANE_NATIVE_SPLIT_APPLY:-0}" \
      "$PY" "$ROOT/scripts/baselane_apply_native_splits.py" \
      --plan "$ROOT/reports/baselane_native_split_plan.json" \
      --report "$ROOT/reports/baselane_native_split_apply_report.json" \
      --state "$ROOT/scripts/.baselane_native_split_apply_state.json" \
      --apply
    native_split_apply_rc="$?"
    set -e
  else
    native_split_apply_rc=0
  fi
  if [ -f "$ROOT/scripts/baselane_native_split_plan.py" ]; then
    set +e
    "$PY" "$ROOT/scripts/baselane_native_split_plan.py" \
      --source-index "$ROOT/reports/baselane_source_transaction_index.csv" \
      --report "$ROOT/reports/baselane_native_split_plan.json" \
      --csv "$ROOT/reports/baselane_native_split_plan.csv" \
      --markdown "$ROOT/reports/baselane_native_split_plan.md" \
      --apply-report "$ROOT/reports/baselane_native_split_apply_report.json"
    native_split_reconcile_rc="$?"
    set -e
  else
    native_split_reconcile_rc=0
  fi
  if [ -f "$ROOT/scripts/clear_future_cf_statement_values.py" ]; then
    set +e
    future_cf_values_apply_args=()
    future_cf_values_period_args=()
    if [ "$BASELANE_FUTURE_CF_VALUES_APPLY" = "1" ]; then
      future_cf_values_apply_args=(--apply)
    fi
    if [ "$BASELANE_WEEKLY_ALLOW_INCOMPLETE_MONTH" = "1" ]; then
      read -r future_cf_values_year future_cf_values_start_month < <(
        "$PY" - "$CF_MONTH" <<'PY'
import sys

year, month = map(int, sys.argv[1].split("-"))
if month == 12:
    year, month = year + 1, 1
else:
    month += 1
print(year, month)
PY
      )
      future_cf_values_period_args=(--year "$future_cf_values_year" --start-month "$future_cf_values_start_month")
    fi
    "$PY" "$ROOT/scripts/clear_future_cf_statement_values.py" \
      --include-archive \
      --include-conflicts \
      "${future_cf_values_period_args[@]}" \
      "${future_cf_values_apply_args[@]}" \
      --report "$ROOT/reports/future_cf_statement_values_apply_report.json" >/dev/null
    future_cf_values_apply_rc="$?"
    "$PY" "$ROOT/scripts/clear_future_cf_statement_values.py" \
      --include-archive \
      --include-conflicts \
      "${future_cf_values_period_args[@]}" \
      --report "$ROOT/reports/future_cf_statement_values_clear_report.json" >/dev/null
    future_cf_values_rc="$?"
    if [ "$future_cf_values_apply_rc" -ne 0 ]; then
      future_cf_values_rc="$future_cf_values_apply_rc"
    fi
    set -e
  else
    future_cf_values_rc=1
  fi
  future_cf_values_apply_changed_cell_count="$($PY - "$ROOT/reports/future_cf_statement_values_apply_report.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    data = json.loads(path.read_text(encoding="utf-8"))
    print(int(data.get("changed_cell_count") or 0))
except Exception:
    print(0)
PY
)"
  future_cf_values_apply_unreadable_count="$($PY - "$ROOT/reports/future_cf_statement_values_apply_report.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    data = json.loads(path.read_text(encoding="utf-8"))
    print(int(data.get("unreadable_count") or 0))
except Exception:
    print(0)
PY
)"
  if [ -f "$ROOT/scripts/baselane_cf_no_gl_property_match_report.py" ]; then
    set +e
    "$PY" "$ROOT/scripts/baselane_cf_no_gl_property_match_report.py" \
      --root "$ROOT" \
      --month "$CF_MONTH" \
      --gl-path "$BASELANE_LEDGER_PATH" >/dev/null
    cf_no_gl_property_match_rc="$?"
    set -e
  else
    cf_no_gl_property_match_rc=0
  fi
      set +e
      "$PY" "$ROOT/scripts/baselane_weekly_cf_review_gate.py" \
    --root "$ROOT" \
    --report "$ROOT/reports/baselane_weekly_cf_review_gate.json" \
    --markdown "$ROOT/reports/baselane_weekly_cf_review_gate.md"
  cf_gate_rc="$?"
  set -e
  weekly_status="$($PY - "$ROOT/reports/baselane_weekly_unprocessed_report.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    print("missing")
else:
    try:
        print(json.loads(path.read_text(encoding="utf-8")).get("status") or "unknown")
    except Exception:
        print("unreadable")
PY
)"
  if [ "$weekly_status" = "NO_REPLY" ]; then
    STATUS="ok"
  else
    STATUS="review"
    REASON="weekly_unprocessed_${weekly_status}"
  fi
  cf_status="$($PY - "$ROOT/reports/baselane_weekly_cf_statement_sync_report.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    print("missing")
else:
    try:
        print(json.loads(path.read_text(encoding="utf-8")).get("status") or "unknown")
    except Exception:
        print("unreadable")
PY
)"
  if [ "$cf_status" != "ok" ]; then
    STATUS="review"
    if [ -n "$REASON" ]; then
      REASON="${REASON};cf_statement_sync_${cf_status}"
    else
      REASON="cf_statement_sync_${cf_status}"
    fi
  fi
  if [ "$cf_gate_rc" -ne 0 ]; then
    STATUS="review"
    if [ -n "$REASON" ]; then
      REASON="${REASON};cf_review_gate_review"
    else
      REASON="cf_review_gate_review"
    fi
  fi
  untagged_review_clean="$($PY - "$ROOT/reports/baselane_cf_untagged_review_packet.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    print("false")
else:
    print(
        "true"
        if data.get("status") == "ok"
        and int(data.get("effective_untagged_row_count", data.get("untagged_row_count")) or 0) == 0
        and int(data.get("effective_review_required_count", data.get("review_required_count")) or 0) == 0
        else "false"
    )
PY
  )"
  if [ "$untagged_review_clean" != "true" ]; then
    STATUS="review"
    append_review_reason "untagged_review_not_clean"
  fi
  if [ "$future_cf_values_rc" -ne 0 ]; then
    STATUS="review"
    append_review_reason "future_cf_values_review"
  fi
  if [ "${future_cf_values_apply_changed_cell_count:-0}" -gt 0 ] || [ "${future_cf_values_apply_unreadable_count:-0}" -gt 0 ]; then
    STATUS="review"
    append_review_reason "future_cf_values_review"
  fi
  if [ "${cf_no_gl_property_match_rc:-0}" -ne 0 ] && [ "$cf_status" = "ok" ]; then
    STATUS="review"
    append_review_reason "cf_no_gl_property_match_review"
  fi
  run_mortgage_workflow_report_refresh
  if [ "$ecogl_autonomy_rc" -ne 0 ]; then
    STATUS="review"
    if [ -n "$REASON" ]; then
      REASON="${REASON};ecogl_data_quality_hold"
    else
      REASON="ecogl_data_quality_hold"
    fi
  fi
  if [ "$ecogl_source_fix_rc" -ne 0 ]; then
    STATUS="review"
    if [ -n "$REASON" ]; then
      REASON="${REASON};ecogl_source_fix_queue"
    else
      REASON="ecogl_source_fix_queue"
    fi
  fi
  if [ "$ecogl_source_fix_evidence_rc" -ne 0 ]; then
    STATUS="review"
    if [ -n "$REASON" ]; then
      REASON="${REASON};ecogl_source_fix_evidence"
    else
      REASON="ecogl_source_fix_evidence"
    fi
  fi
  if [ "$ecogl_source_fix_verifier_rc" -ne 0 ]; then
    STATUS="review"
    if [ -n "$REASON" ]; then
      REASON="${REASON};ecogl_source_fix_verifier"
    else
      REASON="ecogl_source_fix_verifier"
    fi
  fi
  if [ "$ecogl_source_fix_corrections_rc" -ne 0 ]; then
    STATUS="review"
    if [ -n "$REASON" ]; then
      REASON="${REASON};ecogl_source_fix_corrections"
    else
      REASON="ecogl_source_fix_corrections"
    fi
  fi
  if [ "$ecogl_source_fix_approval_rc" -ne 0 ]; then
    STATUS="review"
    if [ -n "$REASON" ]; then
      REASON="${REASON};ecogl_source_fix_approval"
    else
      REASON="ecogl_source_fix_approval"
    fi
  fi
  if [ "$ecogl_source_fix_correction_validation_rc" -ne 0 ]; then
    STATUS="review"
    if [ -n "$REASON" ]; then
      REASON="${REASON};ecogl_source_fix_correction_validation"
    else
      REASON="ecogl_source_fix_correction_validation"
    fi
  fi
  if [ "$ecogl_source_fix_apply_plan_rc" -ne 0 ]; then
    STATUS="review"
    if [ -n "$REASON" ]; then
      REASON="${REASON};ecogl_source_fix_apply_plan"
    else
      REASON="ecogl_source_fix_apply_plan"
    fi
  fi
  if [ "$ecogl_source_fix_apply_rc" -ne 0 ]; then
    STATUS="review"
    if [ -n "$REASON" ]; then
      REASON="${REASON};ecogl_source_fix_apply"
    else
      REASON="ecogl_source_fix_apply"
    fi
  fi
  if [ "$ecogl_source_fix_action_queue_rc" -ne 0 ] && [ "$ecogl_source_fix_action_queue_rc" -ne 2 ]; then
    STATUS="review"
    if [ -n "$REASON" ]; then
      REASON="${REASON};ecogl_source_fix_action_queue"
    else
      REASON="ecogl_source_fix_action_queue"
    fi
  fi
  if [ "$native_split_plan_rc" -ne 0 ] && [ "$native_split_plan_rc" -ne 2 ]; then
    STATUS="review"
    if [ -n "$REASON" ]; then
      REASON="${REASON};native_split_plan"
    else
      REASON="native_split_plan"
    fi
  fi
  if [ "$native_split_apply_rc" -ne 0 ] && [ "$native_split_apply_rc" -ne 2 ]; then
    STATUS="review"
    if [ -n "$REASON" ]; then
      REASON="${REASON};native_split_apply"
    else
      REASON="native_split_apply"
    fi
  fi
  if [ "$native_split_reconcile_rc" -ne 0 ] && [ "$native_split_reconcile_rc" -ne 2 ]; then
    STATUS="review"
    if [ -n "$REASON" ]; then
      REASON="${REASON};native_split_reconcile"
    else
      REASON="native_split_reconcile"
    fi
  fi
  write_report "$STATUS" 0 "$REASON"
  if [ -f "$ROOT/scripts/baselane_reconcile_weekly_report_counts.py" ]; then
    "$PY" "$ROOT/scripts/baselane_reconcile_weekly_report_counts.py" >/dev/null 2>&1 || true
  fi
  weekly_effective_status="$("$PY" - "$ROOT" "$REASON" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
reasons = {part for part in (sys.argv[2] or "").split(";") if part}
reconcilable_reasons = {
    "cf_statement_sync_review",
    "ecogl_data_quality_hold",
    "ecogl_source_fix_queue",
    "ecogl_source_fix_evidence",
    "future_cf_values_review",
}

def read_json(name):
    path = root / "reports" / name
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

def intish(value):
    try:
        return int(value or 0)
    except Exception:
        return 0

weekly = read_json("baselane_weekly_unprocessed_report.json")
cf = read_json("baselane_weekly_cf_statement_sync_report.json")
cf_gate = read_json("baselane_weekly_cf_review_gate.json")
future_audit = read_json("future_cf_statement_values_clear_report.json")
future_apply = read_json("future_cf_statement_values_apply_report.json")
source_verifier = read_json("baselane_ecogl_source_fix_verifier.json")
source_queue = read_json("baselane_ecogl_source_fix_action_queue.json")

unreconciled = reasons - reconcilable_reasons
weekly_clean = (weekly.get("status") or "") == "NO_REPLY"
cf_clean = (
    (cf.get("effective_status") or cf.get("status")) == "ok"
    and intish(cf.get("effective_gate_blocker_count")) == 0
    and intish(cf.get("effective_gate_action_queue_count")) == 0
    and intish(cf.get("source_cash_balance_violation_count")) == 0
    and intish(cf.get("conflict_count")) == 0
)
cf_gate_clean = (cf_gate.get("status") or "") == "ok" and intish(cf_gate.get("blocker_count")) == 0
future_clean = all(
    (report.get("status") or "") == "ok"
    and intish(report.get("changed_cell_count")) == 0
    and intish(report.get("unreadable_count")) == 0
    for report in (future_audit, future_apply)
)
source_clean = (
    (source_verifier.get("status") or "") == "ok"
    and intish(source_verifier.get("remaining_count")) == 0
    and (source_queue.get("status") or "") == "ok"
    and intish(source_queue.get("row_count")) == 0
)
untagged = read_json("baselane_cf_untagged_review_packet.json")
untagged_clean = (
    (untagged.get("status") or "") == "ok"
    and intish(untagged.get("effective_untagged_row_count", untagged.get("untagged_row_count"))) == 0
    and intish(untagged.get("effective_review_required_count", untagged.get("review_required_count"))) == 0
)

print("ok" if not unreconciled and weekly_clean and cf_clean and cf_gate_clean and future_clean and source_clean and untagged_clean else "review")
PY
)"
  if [ "$STATUS" = "review" ] && [ "$weekly_effective_status" = "ok" ]; then
    STATUS="ok"
    REASON=""
  fi
  if [ "$STATUS" = "ok" ]; then
        echo "$ISO_WEEK" > "$STATE_FILE"
        STATE_FILE_MARKED_COMPLETE=true
      else
        DETERMINISTIC_VERIFICATION_IDEMPOTENT=false
        DETERMINISTIC_VERIFICATION_IDEMPOTENT_REASON="review_run_not_marked_complete"
        if [ -f "$STATE_FILE" ] && [ "$(cat "$STATE_FILE" 2>/dev/null || true)" = "$ISO_WEEK" ]; then
          rm -f "$STATE_FILE"
          STATE_FILE_UNMARKED=true
          STATE_FILE_UNMARKED_REASON="latest weekly run is review; do not skip retry before review is clean"
        fi
      fi
  write_report "$STATUS" 0 "$REASON"
  if [ -f "$ROOT/scripts/baselane_reconcile_weekly_report_counts.py" ]; then
    "$PY" "$ROOT/scripts/baselane_reconcile_weekly_report_counts.py" >/dev/null 2>&1 || true
  fi
  exit 0
fi

STATUS="failed"
REASON="hook_returned_nonzero"
run_mortgage_workflow_report_refresh
write_report "$STATUS" "$RETURN_CODE" "$REASON"
exit "$RETURN_CODE"
