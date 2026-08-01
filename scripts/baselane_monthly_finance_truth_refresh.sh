#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${WORKSPACE_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
PY="${PYTHON_BIN:-python3}"
RUN_MONTH="${RUN_MONTH:?RUN_MONTH is required}"
DRY_RUN="${DRY_RUN:-0}"
APPLY_LIVE="${APPLY_BASELANE_MONTHLY_ACCRUALS_LIVE:-0}"
VALIDATE_ONLY="${BASELANE_MONTHLY_FINANCE_TRUTH_VALIDATE_ONLY:-0}"
RUN_WEEKLY="${RUN_BASELANE_MONTHLY_WEEKLY_REFRESH:-1}"
WEEKLY_CF_MONTH="${CF_MONTH:-$RUN_MONTH}"
WEEKLY_LIVE_ACTIONS_APPROVED="${BASELANE_WEEKLY_LIVE_ACTIONS_APPROVED:-$APPLY_LIVE}"
WEEKLY_CF_BALANCE_SHEET_CASH_APPLY="${CF_BALANCE_SHEET_CASH_APPLY:-$APPLY_LIVE}"
WEEKLY_ALLOW_INCOMPLETE_MONTH="${BASELANE_WEEKLY_ALLOW_INCOMPLETE_MONTH:-${BASELANE_MONTHLY_ALLOW_CURRENT_MONTH_TRANSACTION_EXPORT_CLOSE:-0}}"
REPORT_DIR="${BASELANE_REPORT_DIR:-$ROOT/reports}"
LEDGER="${BASELANE_LEDGER_PATH:-/mnt/c/Users/digit/Dropbox/Projects/assetrail/ECO Systems General Ledger.csv}"
# Creating a fresh CDP target opens another visible Baselane tab. Reuse the
# authenticated tab unless an operator explicitly permits a new target.
BASELANE_GQL_CREATE_TARGET="${BASELANE_GQL_CREATE_TARGET:-0}"
export BASELANE_GQL_CREATE_TARGET
MONTH_KEY="${RUN_MONTH/-/}"
STAGE_REPORT="$REPORT_DIR/baselane_monthly_accruals_${MONTH_KEY}.stage.json"
LIVE_PLAN_REPORT="$REPORT_DIR/baselane_monthly_accruals_${MONTH_KEY}.live-plan.json"
LIVE_APPLY_REPORT="$REPORT_DIR/baselane_monthly_accruals_${MONTH_KEY}.live-apply.json"
LIVE_VERIFY_REPORT="$REPORT_DIR/baselane_monthly_accruals_${MONTH_KEY}.live-verify.json"
ALIGNED_STAGE_REPORT="$REPORT_DIR/baselane_aligned_owner_statement_import_${MONTH_KEY}.finance-truth-stage.json"
INSURANCE_CLEANUP_REPORT="${BASELANE_CASH_BASIS_INSURANCE_CLEANUP_REPORT:-$REPORT_DIR/obie_cash_basis_insurance_cleanup_${MONTH_KEY}.json}"
INSURANCE_CLEANUP_SOURCE_INDEX="${BASELANE_SOURCE_TRANSACTION_INDEX:-$REPORT_DIR/baselane_source_transaction_index.csv}"
INSURANCE_CLEANUP_REAL_ESTATE_ROOT="${BASELANE_REAL_ESTATE_ROOT:-/mnt/c/Users/digit/Dropbox/Real Estate}"
INSURANCE_CLEANUP_MD="${BASELANE_CASH_BASIS_INSURANCE_CLEANUP_MD:-/mnt/c/Users/digit/Dropbox/Real Estate/Lofty PM/Obie/OH-IL-TN Cash-Basis Insurance Duplicate Audit.md}"
INSURANCE_CLEANUP_CSV="${BASELANE_CASH_BASIS_INSURANCE_CLEANUP_CSV:-/mnt/c/Users/digit/Dropbox/Real Estate/Lofty PM/Obie/OH-IL-TN Cash-Basis Insurance Duplicate Audit.csv}"
REPORT_FILE="${BASELANE_MONTHLY_FINANCE_TRUTH_REFRESH_REPORT:-$REPORT_DIR/baselane_monthly_finance_truth_refresh.json}"
STEP_ERROR_FILE="${BASELANE_MONTHLY_FINANCE_TRUTH_REFRESH_ERROR_FILE:-$REPORT_DIR/baselane_monthly_finance_truth_refresh.last_error.txt}"
REUSE_VERIFIED_MAX_MINUTES="${BASELANE_MONTHLY_FINANCE_TRUTH_REUSE_VERIFY_MAX_MINUTES:-240}"
LIVE_PLAN_TIMEOUT_SECONDS="${BASELANE_MONTHLY_FINANCE_TRUTH_LIVE_PLAN_TIMEOUT_SECONDS:-180}"
AUTH_RECOVERY_SCRIPT="${BASELANE_AUTH_RECOVERY_SCRIPT:-$ROOT/scripts/baselane_cdp_auth_recovery.py}"
AUTH_RECOVERY_REPORT="${BASELANE_AUTH_RECOVERY_REPORT:-$REPORT_DIR/baselane_auth_recovery_report.json}"
AUTH_RECOVERY_WAIT_SECONDS="${BASELANE_AUTH_RECOVERY_WAIT_SECONDS:-3}"
CDP_PROBE_TIMEOUT_SECONDS="${BASELANE_CDP_PROBE_TIMEOUT_SECONDS:-4}"
CURRENT_STEP="preflight"
STAGED_LEDGER=""

# The earlier staging steps can write to both Baselane and shared property
# documents. They must never write merely because this script was invoked:
# live writes require the same explicit approval as the final accrual apply.
LIVE_WRITE_MODE=0
if [ "$DRY_RUN" != "1" ] && [ "$VALIDATE_ONLY" != "1" ] && [ "$APPLY_LIVE" = "1" ]; then
  LIVE_WRITE_MODE=1
fi

cdp_reachable() {
  command -v curl >/dev/null 2>&1 && timeout "$CDP_PROBE_TIMEOUT_SECONDS" curl -fsS -H 'Host: localhost' "$1" >/dev/null 2>&1
}

cdp_json() {
  timeout "$CDP_PROBE_TIMEOUT_SECONDS" curl -fsS -H 'Host: localhost' "$1"
}

cdp_has_baselane_tab() {
  local version_url="$1"
  local base_url="${version_url%/json/version}"
  command -v curl >/dev/null 2>&1 || return 1
  cdp_json "$base_url/json/list" 2>/dev/null | "$PY" -c 'import json, sys
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
  cdp_json "$base_url/json/list" 2>/dev/null | "$PY" -c 'import json, sys
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
    cyber_ts_ip="$(timeout 3 tailscale ip --4 cyber 2>/dev/null || true)"
  fi
  if [ -z "$cyber_ts_ip" ]; then
    cyber_ts_ip="100.115.208.70"
  fi

  local candidates=(
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

mkdir -p "$REPORT_DIR"
: > "$STEP_ERROR_FILE"
cleanup() {
  rc="$?"
  if [ -n "$STAGED_LEDGER" ]; then
    rm -f "$STAGED_LEDGER"
  fi
  if [ "$rc" -ne 0 ]; then
    "$PY" - "$REPORT_FILE" "$RUN_MONTH" "$DRY_RUN" "$CURRENT_STEP" "$rc" "$STAGE_REPORT" "$LIVE_PLAN_REPORT" "$LIVE_APPLY_REPORT" "$LIVE_VERIFY_REPORT" "$STEP_ERROR_FILE" "$ALIGNED_STAGE_REPORT" "$AUTH_RECOVERY_REPORT" <<'PY' || true
import datetime as dt
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
error_path = Path(sys.argv[10])
aligned_stage_report = sys.argv[11]
auth_recovery_report = Path(sys.argv[12])

def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}

error_text = ""
try:
    error_text = error_path.read_text(encoding="utf-8", errors="replace").strip()
except OSError:
    error_text = ""
auth_recovery = read_json(auth_recovery_report)
stage_source_report = read_json(Path(sys.argv[6]))
error_tail = "\n".join(error_text.splitlines()[-30:]) if error_text else None
auth_markers = (
    "NO_USABLE_BASELANE_TARGET",
    "recaptcha",
    "captcha",
    "x-firebase-appcheck",
    "baselane_login_required",
    "UNAUTHORIZED_ACCESS",
    "Missing cookie",
    "no direct authenticated-looking Baselane page targets",
    "Existing Baselane tabs are login/error pages",
)
cdp_timeout_markers = (
    "TimeoutExpired",
    "BASELANE_LIVE_PLAN_TIMEOUT",
    "timeout: Runtime.evaluate",
    "timeout: Network.enable",
    "timeout: Page.reload",
    "Inspected target navigated or closed",
)
auth_blocked = any(marker in error_text for marker in auth_markers)
cdp_blocked = auth_blocked or any(marker in error_text for marker in cdp_timeout_markers) or (
    "baselane_graphql_via_cdp.js" in error_text and "timeout" in error_text.lower()
)
issue_summary = None
next_action = None
if auth_blocked:
    issue_summary = "Baselane auth/appcheck blocked monthly finance-truth live accrual planning."
    next_action = (
        "Solve Baselane reCAPTCHA/appcheck or complete login in the visible Baselane CDP tab, "
        f"then rerun `RUN_MONTH={sys.argv[2]} bash scripts/baselane_monthly_finance_truth_refresh.sh` "
        "or `bash scripts/baselane_financials_post_auth_resume.sh` before downstream "
        "CF/FINANCIALS/Lofty/Discord/email outputs."
    )
elif cdp_blocked:
    issue_summary = "Baselane CDP protocol timeout blocked monthly finance-truth live accrual planning."
    next_action = (
        "Resolve the Baselane CDP timeout or reopen the visible Baselane browser tab, "
        f"then rerun `RUN_MONTH={sys.argv[2]} bash scripts/baselane_monthly_finance_truth_refresh.sh` "
        "before downstream CF/FINANCIALS/Lofty/Discord/email outputs."
    )
elif sys.argv[4] == "stage_monthly_accruals":
    preflight = stage_source_report.get("preflight_failure")
    if isinstance(preflight, dict):
        code = str(preflight.get("code") or "accrual_stage_preflight_blocked")
        message = str(preflight.get("message") or "Monthly accrual staging preflight failed.")
        missing_reserves = preflight.get("missing_lofty_reserves")
        issue_summary = f"Monthly accrual staging blocked: {message}"
        if code == "lofty_reserve_properties_missing" and isinstance(missing_reserves, list):
            names = ", ".join(str(item) for item in missing_reserves if str(item).strip())
            diagnostics = preflight.get("missing_lofty_reserve_diagnostics")
            absent_from_roster = []
            if isinstance(diagnostics, list):
                absent_from_roster = [
                    str(item.get("property"))
                    for item in diagnostics
                    if isinstance(item, dict)
                    and item.get("status") == "absent_from_live_manager_roster"
                    and str(item.get("property") or "").strip()
                ]
            if absent_from_roster:
                absent_names = ", ".join(absent_from_roster)
                issue_summary = (
                    "Monthly accrual staging blocked: active DAO(s) absent from the live Lofty manager roster: "
                    f"{absent_names}."
                )
                next_action = (
                    "Restore the active property to Lofty manager access or capture a fresh authoritative "
                    f"get-manager-properties reserve snapshot for {absent_names}, then rerun "
                    f"`RUN_MONTH={sys.argv[2]} bash scripts/baselane_monthly_finance_truth_refresh.sh`. "
                    "Do not publish CF/FINANCIALS/Lofty/Discord/email outputs until this evidence is complete."
                )
            else:
                next_action = (
                    "Refresh the read-only Lofty get-manager-properties snapshot and verify it contains "
                    f"curr_maintenance_reserve for {names or 'every retained-capital property'}, then rerun "
                    f"`RUN_MONTH={sys.argv[2]} bash scripts/baselane_monthly_finance_truth_refresh.sh`. "
                    "Do not publish CF/FINANCIALS/Lofty/Discord/email outputs until this evidence is complete."
                )
        else:
            next_action = (
                f"Resolve monthly accrual staging preflight `{code}`, then rerun "
                f"`RUN_MONTH={sys.argv[2]} bash scripts/baselane_monthly_finance_truth_refresh.sh` before downstream outputs."
            )
    else:
        amount_mismatches = stage_source_report.get("amount_mismatches") or []
        amount_mismatch_count = int(stage_source_report.get("amount_mismatch_count") or 0)
        if amount_mismatch_count:
            properties = []
            for mismatch in amount_mismatches:
                if not isinstance(mismatch, dict):
                    continue
                name = str(mismatch.get("property") or "").strip()
                if name and name not in properties:
                    properties.append(name)
            names = ", ".join(properties)
            issue_summary = (
                f"Monthly accrual staging blocked by {amount_mismatch_count} retained/accrual amount "
                f"mismatch(es){': ' + names if names else ''}."
            )
            next_action = (
                "Obtain accounting evidence for each staged amount mismatch and record an exact approved "
                "exception where appropriate. Do not apply a generic normalization or create a Baselane "
                f"source transaction from the Cash Flow value; then rerun `RUN_MONTH={sys.argv[2]} "
                "bash scripts/baselane_monthly_finance_truth_refresh.sh`."
            )
report = {
    "job": "baselane-monthly-finance-truth-refresh",
    "generated_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "status": "failed",
    "run_month": sys.argv[2],
    "dry_run": sys.argv[3] == "1",
    "failed_step": sys.argv[4],
    "exit_code": int(sys.argv[5]),
    "stage_report": sys.argv[6],
    "stage_source_status": stage_source_report.get("status"),
    "stage_preflight_failure": stage_source_report.get("preflight_failure"),
    "live_plan_report": sys.argv[7],
    "live_apply_report": sys.argv[8],
    "live_verify_report": sys.argv[9],
    "aligned_owner_stage_report": aligned_stage_report,
    "auth_recovery_report": str(auth_recovery_report),
    "auth_recovery_status": auth_recovery.get("status"),
    "auth_recovery_issue_summary": auth_recovery.get("issue_summary"),
    "auth_recovery_manual_auth_required": auth_recovery.get("manual_auth_required"),
    "auth_recovery_manual_auth_reason": auth_recovery.get("manual_auth_reason"),
    "auth_recovery_login_attempt_count": auth_recovery.get("login_recovery_attempt_count"),
    "auth_recovery_hard_refresh_attempt_count": auth_recovery.get("login_recovery_hard_refresh_attempt_count"),
    "auth_recovery_closed_app_tab_count": auth_recovery.get("login_recovery_closed_app_tab_count"),
    "auth_recovery_opened_statement_tab_count": auth_recovery.get("login_recovery_opened_statement_tab_count"),
    "auth_recovery_next_action": auth_recovery.get("next_action"),
    "error_file": str(error_path),
    "error_tail": error_tail,
    "auth_blocked": auth_blocked or cdp_blocked,
    "cdp_blocked": cdp_blocked,
    "issue_summary": issue_summary,
    "next_action": next_action,
    "safe_to_retry_after_manual_auth": bool(auth_blocked or cdp_blocked),
    "canonical_local_accrual_write": False,
    "downstream_generation_allowed": False,
}
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
step_artifacts = {
    "stage_monthly_accruals": Path(sys.argv[6]),
    "live_accrual_plan": Path(sys.argv[7]),
    "live_accrual_apply": Path(sys.argv[8]),
    "post_apply_live_verify": Path(sys.argv[9]),
}
step_artifact = step_artifacts.get(sys.argv[4])
if step_artifact is not None:
    step_report = dict(report)
    step_report["status"] = "failed"
    step_report["stale_success_invalidated"] = True
    step_report["truth_refresh_report"] = str(path)
    step_artifact.parent.mkdir(parents=True, exist_ok=True)
    step_artifact.write_text(json.dumps(step_report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
  fi
  exit "$rc"
}
trap cleanup EXIT

if [ ! -f "$LEDGER" ]; then
  echo "[baselane-monthly-truth] ledger not found: $LEDGER" >&2
  exit 1
fi

configure_baselane_cdp

STAGED_LEDGER="$(mktemp --tmpdir baselane-monthly-accrual-stage.XXXXXX.csv)"
cp -- "$LEDGER" "$STAGED_LEDGER"

CURRENT_STEP="cash_basis_insurance_cleanup"
insurance_cleanup_args=(
  "$ROOT/scripts/obie_cash_basis_insurance_cleanup.py"
  --gl-csv "$STAGED_LEDGER"
  --real-estate-root "$INSURANCE_CLEANUP_REAL_ESTATE_ROOT"
  --source-index "$INSURANCE_CLEANUP_SOURCE_INDEX"
  --report-json "$INSURANCE_CLEANUP_REPORT"
  --report-md "$INSURANCE_CLEANUP_MD"
  --report-csv "$INSURANCE_CLEANUP_CSV"
)
if [ "$LIVE_WRITE_MODE" = "1" ]; then
  insurance_cleanup_args+=(--apply-local --apply-live)
fi
set +e
"$PY" "${insurance_cleanup_args[@]}" >/dev/null 2>"$STEP_ERROR_FILE"
insurance_cleanup_rc="$?"
set -e
if [ "$insurance_cleanup_rc" -ne 0 ]; then
  cat "$STEP_ERROR_FILE" >&2 || true
  echo "[baselane-monthly-truth] cash-basis insurance cleanup failed; refusing downstream generation" >&2
  exit "$insurance_cleanup_rc"
fi

CURRENT_STEP="stage_aligned_owner_detail"
if [ "${BASELANE_MONTHLY_FINANCE_TRUTH_STAGE_ALIGNED_OWNER_IMPORT:-1}" = "1" ] && [ -x "$ROOT/scripts/baselane_aligned_owner_statement_import.py" ]; then
  aligned_args=(
    "$ROOT/scripts/baselane_aligned_owner_statement_import.py"
    --month "$RUN_MONTH"
    --report "$ALIGNED_STAGE_REPORT"
    --manifest-dir "$REPORT_DIR/aligned-owner-statement-import-manifests"
    --convert
    --skip-baselane-query
    --skip-settlement-relabels
    --stage-ledger "$STAGED_LEDGER"
  )
  if [ "$LIVE_WRITE_MODE" = "1" ] && [ "${BASELANE_ALIGNED_OWNER_IMPORT_PUBLISH_CONVERTED:-1}" = "1" ]; then
    aligned_args+=(--publish-converted)
  fi
  "$PY" "${aligned_args[@]}" >/dev/null
fi

CURRENT_STEP="stage_monthly_accruals"
set +e
BASELANE_LEDGER_PATH="$STAGED_LEDGER" \
BASELANE_MONTHLY_ACCRUALS_MONTH="$RUN_MONTH" \
BASELANE_MONTHLY_ACCRUALS_DRY_RUN="$((1 - LIVE_WRITE_MODE))" \
BASELANE_MONTHLY_ACCRUALS_APPLY="$LIVE_WRITE_MODE" \
BASELANE_MONTHLY_ACCRUALS_REVIEW_MARKDOWN="$REPORT_DIR/baselane_monthly_accruals_${MONTH_KEY}_stage_review.md" \
"$ROOT/scripts/baselane_monthly_accruals_cron.sh" >/dev/null
stage_rc="$?"
set -e
cp -- "$REPORT_DIR/baselane_monthly_accruals_${MONTH_KEY}.json" "$STAGE_REPORT"
"$PY" - "$STAGE_REPORT" "$stage_rc" <<'PY'
import json
import sys
from pathlib import Path

report_path = Path(sys.argv[1])
stage_rc = int(sys.argv[2])
try:
    report = json.loads(report_path.read_text(encoding="utf-8"))
except Exception as exc:
    raise SystemExit(f"stage report unreadable: {exc}")

status = str(report.get("status") or "").strip().lower()
blocker_fields = {
    "amount_mismatch_count": int(report.get("amount_mismatch_count") or 0),
    "blocked_first_day_pm_fee_count": int(report.get("blocked_first_day_pm_fee_count") or 0),
    "blocking_gap_action_count": int(report.get("blocking_gap_action_count") or 0),
    "missing_fixed_accrual_coverage_count": int(report.get("missing_fixed_accrual_coverage_count") or 0),
    "unapproved_pm_fee_basis_gap_count": int(
        report.get("unapproved_pm_fee_basis_gap_count")
        if report.get("unapproved_pm_fee_basis_gap_count") is not None
        else report.get("pm_fee_basis_gap_count") or 0
    ),
}
gap_approvals = report.get("gap_approvals") if isinstance(report.get("gap_approvals"), dict) else {}
gap_approval_issues = int(gap_approvals.get("issue_count") or 0)
blockers = [f"{key}={value}" for key, value in blocker_fields.items() if value]
if gap_approval_issues:
    blockers.append(f"gap_approval_issue_count={gap_approval_issues}")
gap_actions = report.get("gap_action_queue") if isinstance(report.get("gap_action_queue"), list) else []
audit_only_review = (
    status == "review"
    and bool(gap_actions)
    and all(
        isinstance(action, dict) and str(action.get("severity") or "").strip().lower() == "audit_only"
        for action in gap_actions
    )
)
if status == "ok" or audit_only_review:
    if blockers:
        raise SystemExit(
            "staged accrual completeness failed; "
            f"stage_rc={stage_rc} status={status or 'missing'} {' '.join(blockers)}"
        )
    if audit_only_review:
        report["stage_audit_only_review_accepted"] = True
        report["stage_audit_only_review_count"] = len(gap_actions)
    if stage_rc:
        report["stage_command_rc_nonfatal"] = stage_rc
        report["stage_command_rc_nonfatal_reason"] = (
            "structured stage report is audit-only review with no blocking accrual counts"
            if audit_only_review
            else "structured stage report is ok with no blocking accrual counts"
        )
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    raise SystemExit(0)
raise SystemExit(
    "staged accrual completeness failed; "
    f"stage_rc={stage_rc} status={status or 'missing'} "
    f"{' '.join(blockers) if blockers else 'no_counted_blockers'}"
)
PY

target_digest="$($PY - "$ROOT" "$STAGED_LEDGER" "$RUN_MONTH" <<'PY'
import importlib.util
import os
import sys
from pathlib import Path

override = os.environ.get("BASELANE_MONTHLY_FINANCE_TRUTH_EXPECTED_TARGET_DIGEST")
if override:
    print(override)
    raise SystemExit(0)
root = Path(sys.argv[1])
module_path = root / "scripts" / "baselane_apply_monthly_accruals_live.py"
try:
    spec = importlib.util.spec_from_file_location("baselane_apply_monthly_accruals_live", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("missing module spec")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    print(module.target_digest(module.read_targets(Path(sys.argv[2]), sys.argv[3])))
except BaseException:
    print("")
PY
)"

CURRENT_STEP="live_accrual_plan"
set +e
timeout --kill-after=30s "${LIVE_PLAN_TIMEOUT_SECONDS}s" \
"$PY" "$ROOT/scripts/baselane_apply_monthly_accruals_live.py" \
  --gl-csv "$STAGED_LEDGER" \
  --month "$RUN_MONTH" \
  --report "$LIVE_PLAN_REPORT" >/dev/null 2>"$STEP_ERROR_FILE"
plan_rc="$?"
set -e
if [ "$plan_rc" -eq 124 ]; then
  printf 'BASELANE_LIVE_PLAN_TIMEOUT after %ss\n' "$LIVE_PLAN_TIMEOUT_SECONDS" >"$STEP_ERROR_FILE"
fi
if [ "$plan_rc" -ne 0 ]; then
  cat "$STEP_ERROR_FILE" >&2 || true
  if [ "${BASELANE_MONTHLY_FINANCE_TRUTH_AUTH_RECOVERY:-1}" = "1" ] && [ -x "$AUTH_RECOVERY_SCRIPT" ] && "$PY" - "$STEP_ERROR_FILE" <<'PY'
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace") if Path(sys.argv[1]).exists() else ""
markers = (
    "NO_USABLE_BASELANE_TARGET",
    "recaptcha",
    "captcha",
    "x-firebase-appcheck",
    "baselane_login_required",
    "UNAUTHORIZED_ACCESS",
    "Missing cookie",
    "no direct authenticated-looking Baselane page targets",
    "Existing Baselane tabs are login/error pages",
)
raise SystemExit(0 if any(marker in text for marker in markers) else 1)
PY
  then
    echo "[baselane-monthly-truth] live accrual plan auth-blocked; attempting Baselane CDP recovery once" >&2
    set +e
    "$PY" "$AUTH_RECOVERY_SCRIPT" \
      --recover-login \
      --graphql-auth-smoke \
      --recovery-wait-seconds "$AUTH_RECOVERY_WAIT_SECONDS" \
      --report "$AUTH_RECOVERY_REPORT" >>"$STEP_ERROR_FILE" 2>&1
    auth_recovery_rc="$?"
    set -e
    if [ "$auth_recovery_rc" -eq 0 ]; then
      echo "[baselane-monthly-truth] Baselane CDP recovery returned ok; retrying live accrual plan once" >&2
      set +e
      timeout --kill-after=30s "${LIVE_PLAN_TIMEOUT_SECONDS}s" \
      "$PY" "$ROOT/scripts/baselane_apply_monthly_accruals_live.py" \
        --gl-csv "$STAGED_LEDGER" \
        --month "$RUN_MONTH" \
        --report "$LIVE_PLAN_REPORT" >/dev/null 2>"$STEP_ERROR_FILE"
      plan_rc="$?"
      set -e
      if [ "$plan_rc" -eq 124 ]; then
        printf 'BASELANE_LIVE_PLAN_TIMEOUT after %ss\n' "$LIVE_PLAN_TIMEOUT_SECONDS" >"$STEP_ERROR_FILE"
      fi
      if [ "$plan_rc" -ne 0 ]; then
        cat "$STEP_ERROR_FILE" >&2 || true
      fi
    else
      echo "[baselane-monthly-truth] Baselane CDP recovery still requires review; rc=$auth_recovery_rc" >&2
    fi
  fi
fi
if [ "$plan_rc" -ne 0 ]; then
  if [ -n "$target_digest" ]; then
    set +e
    "$PY" - "$REPORT_FILE" "$RUN_MONTH" "$DRY_RUN" "$target_digest" "$STAGE_REPORT" "$LIVE_PLAN_REPORT" "$LIVE_APPLY_REPORT" "$LIVE_VERIFY_REPORT" "$STEP_ERROR_FILE" "$REUSE_VERIFIED_MAX_MINUTES" "$ALIGNED_STAGE_REPORT" <<'PY'
import datetime as dt
import json
import sys
from pathlib import Path

report_path = Path(sys.argv[1])
run_month = sys.argv[2]
dry_run = sys.argv[3] == "1"
target_digest = sys.argv[4]
stage_report = sys.argv[5]
live_plan_path = Path(sys.argv[6])
live_apply_path = Path(sys.argv[7])
live_verify_path = Path(sys.argv[8])
error_path = Path(sys.argv[9])
max_minutes = int(sys.argv[10])
aligned_stage_report = sys.argv[11]

def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

def parse_generated_at(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

error_text = error_path.read_text(encoding="utf-8", errors="replace") if error_path.exists() else ""
auth_markers = (
    "NO_USABLE_BASELANE_TARGET",
    "recaptcha",
    "captcha",
    "x-firebase-appcheck",
    "baselane_login_required",
    "UNAUTHORIZED_ACCESS",
    "Missing cookie",
    "no direct authenticated-looking Baselane page targets",
    "Existing Baselane tabs are login/error pages",
)
cdp_timeout_markers = (
    "TimeoutExpired",
    "BASELANE_LIVE_PLAN_TIMEOUT",
    "timeout: Runtime.evaluate",
    "timeout: Network.enable",
    "timeout: Page.reload",
    "Inspected target navigated or closed",
)
auth_blocked = any(marker in error_text for marker in auth_markers)
cdp_blocked = auth_blocked or any(marker in error_text for marker in cdp_timeout_markers) or (
    "baselane_graphql_via_cdp.js" in error_text and "timeout" in error_text.lower()
)
if not cdp_blocked:
    raise SystemExit(1)

live_verify = read_json(live_verify_path)
live_apply = read_json(live_apply_path)
now = dt.datetime.now(dt.UTC)
generated_at = parse_generated_at(str(live_verify.get("generated_at") or ""))
fresh = generated_at is not None and (now - generated_at).total_seconds() <= max_minutes * 60
same_digest = live_verify.get("target_digest") == target_digest and (
    not live_apply.get("target_digest") or live_apply.get("target_digest") == target_digest
)
verified_clean = (
    live_verify.get("status") == "ok"
    and int(live_verify.get("target_count") or 0) > 0
    and int(live_verify.get("skip_count") or 0) == int(live_verify.get("target_count") or 0)
    and int(live_verify.get("create_count") or 0) == 0
    and int(live_verify.get("update_count") or 0) == 0
    and int(live_verify.get("issue_count") or 0) == 0
)
if not (fresh and same_digest and verified_clean):
    raise SystemExit(1)

generated = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
error_tail = "\n".join(error_text.splitlines()[-30:]) if error_text else None
live_plan = {
    "job": "baselane-monthly-accrual-live-plan",
    "generated_at": generated,
    "status": "ok",
    "run_month": run_month,
    "target_digest": target_digest,
    "live_verify_report": str(live_verify_path),
    "live_apply_report": str(live_apply_path),
    "auth_blocked": False,
    "cdp_blocked": False,
    "observed_auth_blocked": auth_blocked,
    "observed_cdp_blocked": cdp_blocked,
    "reuse_verified_max_minutes": max_minutes,
    "reason": "live accrual planning was auth-blocked, but a fresh matching live apply+verify report proves idempotent current state",
}
live_plan_path.parent.mkdir(parents=True, exist_ok=True)
live_plan_path.write_text(json.dumps(live_plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")

report = {
    "job": "baselane-monthly-finance-truth-refresh",
    "generated_at": generated,
    "status": "ok",
    "run_month": run_month,
    "dry_run": dry_run,
    "target_digest": target_digest,
    "stage_report": stage_report,
    "aligned_owner_stage_report": aligned_stage_report,
    "live_plan_report": str(live_plan_path),
    "live_apply_report": str(live_apply_path),
    "live_verify_report": str(live_verify_path),
    "live_apply_status": "reused_prior_live_verify_after_auth_block",
    "post_apply_sync_status": "reused_prior_live_verify_after_auth_block",
    "weekly_split_cf_refresh_status": "skipped_reused_prior_verified_after_auth_block",
    "canonical_local_accrual_write": False,
    "auth_blocked": False,
    "cdp_blocked": False,
    "observed_auth_blocked": auth_blocked,
    "observed_cdp_blocked": cdp_blocked,
    "error_file": str(error_path),
    "error_tail": error_tail,
    "reuse_verified_max_minutes": max_minutes,
    "downstream_generation_allowed": True,
}
report_path.parent.mkdir(parents=True, exist_ok=True)
report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
    reuse_rc="$?"
    set -e
    if [ "$reuse_rc" -eq 0 ]; then
      echo "[baselane-monthly-truth] live accrual plan auth-blocked; reusing fresh matching live apply+verify evidence" >&2
      exit 0
    fi
  fi
  echo "[baselane-monthly-truth] live accrual plan is blocked; refusing downstream generation" >&2
  exit "$plan_rc"
fi

plan_target_digest="$($PY - "$LIVE_PLAN_REPORT" <<'PY'
import json
import sys
from pathlib import Path
print(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["target_digest"])
PY
)"
if [ -z "$target_digest" ]; then
  target_digest="$plan_target_digest"
elif [ "$plan_target_digest" != "$target_digest" ]; then
  echo "[baselane-monthly-truth] live plan target digest changed after staged target calculation" >&2
  exit 2
fi

live_apply_status="skipped_dry_run"
sync_status="skipped_dry_run"
weekly_status="skipped_dry_run"
if [ "$DRY_RUN" != "1" ]; then
  if [ "$VALIDATE_ONLY" = "1" ]; then
    CURRENT_STEP="live_accrual_validate_only"
    "$PY" "$ROOT/scripts/baselane_apply_monthly_accruals_live.py" \
      --gl-csv "$LEDGER" \
      --month "$RUN_MONTH" \
      --report "$LIVE_VERIFY_REPORT" \
      --require-target-digest "$target_digest" >/dev/null 2>"$STEP_ERROR_FILE"
    live_apply_status="skipped_validate_only"
    sync_status="skipped_validate_only"
  else
    CURRENT_STEP="live_accrual_apply"
    if [ "$APPLY_LIVE" != "1" ]; then
      echo "[baselane-monthly-truth] live run requires APPLY_BASELANE_MONTHLY_ACCRUALS_LIVE=1" >&2
      exit 2
    fi
    "$PY" "$ROOT/scripts/baselane_apply_monthly_accruals_live.py" \
      --gl-csv "$STAGED_LEDGER" \
      --month "$RUN_MONTH" \
      --report "$LIVE_APPLY_REPORT" \
      --require-target-digest "$target_digest" \
      --apply >/dev/null 2>"$STEP_ERROR_FILE"
    live_apply_status="ok"

    BASELANE_LEDGER_PATH="$LEDGER" \
    BASELANE_PAGE_LIMIT="${BASELANE_PAGE_LIMIT:-1000}" \
    "$PY" "$ROOT/scripts/baselane_sync_cdp_deterministic.py" >/dev/null
    sync_status="ok"

    CURRENT_STEP="post_apply_live_verify"
    "$PY" "$ROOT/scripts/baselane_apply_monthly_accruals_live.py" \
      --gl-csv "$LEDGER" \
      --month "$RUN_MONTH" \
      --report "$LIVE_VERIFY_REPORT" >/dev/null 2>"$STEP_ERROR_FILE"
  fi
  "$PY" - "$LIVE_VERIFY_REPORT" "$target_digest" <<'PY'
import json
import sys
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
expected_digest = sys.argv[2]
if report.get("status") != "ok":
    raise SystemExit("post-sync live verification status is not ok")
if report.get("target_digest") != expected_digest:
    raise SystemExit("post-sync target digest changed")
if int(report.get("skip_count") or 0) != int(report.get("target_count") or 0):
    raise SystemExit("post-sync canonical ledger does not exactly match live accrual targets")
if int(report.get("create_count") or 0) or int(report.get("update_count") or 0):
    raise SystemExit("post-sync live verification still proposes mutations")
PY

  if [ "$VALIDATE_ONLY" != "1" ]; then
    "$PY" - "$REPORT_DIR/baselane_monthly_accrual_accidental_apply_audit.json" "$RUN_MONTH" "$LEDGER" "$LIVE_VERIFY_REPORT" "$target_digest" <<'PY'
import datetime as dt
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = {
    "generated_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "status": "superseded_by_guarded_live_baselane_sync",
    "run_month": sys.argv[2],
    "current": sys.argv[3],
    "live_verify_report": sys.argv[4],
    "target_digest": sys.argv[5],
    "live_verify_status": "ok",
    "post_apply_sync_status": "ok",
    "reason": "The canonical ledger was re-exported from live Baselane and every guarded accrual target was verified idempotent after sync; the pre-sync local append baseline is no longer authoritative.",
}
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
  fi

  if [ "$RUN_WEEKLY" = "1" ]; then
    CURRENT_STEP="weekly_split_cf_refresh"
    CF_MONTH="$WEEKLY_CF_MONTH" \
      BASELANE_WEEKLY_LIVE_ACTIONS_APPROVED="$WEEKLY_LIVE_ACTIONS_APPROVED" \
      CF_BALANCE_SHEET_CASH_APPLY="$WEEKLY_CF_BALANCE_SHEET_CASH_APPLY" \
      BASELANE_WEEKLY_ALLOW_INCOMPLETE_MONTH="$WEEKLY_ALLOW_INCOMPLETE_MONTH" \
      FORCE_WEEKLY_PASS=1 \
      "$ROOT/scripts/baselane_weekly_file_updates_cron.sh" >/dev/null
    weekly_status="ok"
  else
    weekly_status="skipped_disabled"
  fi
fi

CURRENT_STEP="write_success_report"
"$PY" - "$REPORT_FILE" "$RUN_MONTH" "$DRY_RUN" "$target_digest" "$STAGE_REPORT" "$LIVE_PLAN_REPORT" "$LIVE_APPLY_REPORT" "$LIVE_VERIFY_REPORT" "$live_apply_status" "$sync_status" "$weekly_status" "$ALIGNED_STAGE_REPORT" "$VALIDATE_ONLY" <<'PY'
import datetime as dt
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
report = {
    "job": "baselane-monthly-finance-truth-refresh",
    "generated_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "status": "ok_dry_run" if sys.argv[3] == "1" else "ok",
    "run_month": sys.argv[2],
    "dry_run": sys.argv[3] == "1",
    "target_digest": sys.argv[4],
    "stage_report": sys.argv[5],
    "aligned_owner_stage_report": sys.argv[12],
    "validate_only": sys.argv[13] == "1",
    "live_plan_report": sys.argv[6],
    "live_apply_report": sys.argv[7],
    "live_verify_report": sys.argv[8],
    "live_apply_status": sys.argv[9],
    "post_apply_sync_status": sys.argv[10],
    "weekly_split_cf_refresh_status": sys.argv[11],
    "canonical_local_accrual_write": False,
    "downstream_generation_allowed": True,
}
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

echo "[baselane-monthly-truth] finance truth refresh complete: $REPORT_FILE"
