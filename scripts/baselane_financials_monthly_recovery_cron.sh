#!/usr/bin/env bash
set -euo pipefail

export PATH="${HOME}/.local/bin:/home/digit/.npm-global/bin:${HOME}/.npm-global/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${WORKSPACE_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
PY="${PYTHON_BIN:-python3}"
REPORT_DIR="${BASELANE_REPORT_DIR:-$ROOT/reports}"
MONTHLY_RUN_REPORT="${BASELANE_MONTHLY_RUN_REPORT:-$REPORT_DIR/baselane_financials_monthly_run_report.json}"
MONTHLY_TRUTH_REPORT="${BASELANE_MONTHLY_TRUTH_REPORT:-$REPORT_DIR/baselane_monthly_finance_truth_refresh.json}"
RECOVERY_REPORT="${BASELANE_MONTHLY_RECOVERY_REPORT:-$REPORT_DIR/baselane_financials_monthly_recovery_cron.json}"
SENT_STATE_FILE="${OWNER_EMAIL_SENT_STATE_FILE:-$ROOT/scripts/.baselane_financials_monthly_state/owner_email_sent_month}"
LOCK_PATH="${BASELANE_MONTHLY_LOCK_PATH:-/home/digit/.cache/openclaw-locks/baselane-monthly-v2.lock}"
CURRENT_LOCAL_DATE="${CURRENT_LOCAL_DATE:-$(date +%F)}"
CURRENT_LOCAL_DAY="${CURRENT_LOCAL_DAY:-$(date +%d)}"
LATE_CLOSE_APPROVED="${BASELANE_MONTHLY_LATE_CLOSE_APPROVED:-0}"
export DRY_RUN="${DRY_RUN:-0}"
export BASELANE_MONTHLY_LIVE_ACTIONS_APPROVED="${BASELANE_MONTHLY_LIVE_ACTIONS_APPROVED:-1}"
export AUTO_APPROVE_SAFE_REVIEW_CANDIDATES="${AUTO_APPROVE_SAFE_REVIEW_CANDIDATES:-1}"
export PUBLISH_LOFTY_PM_UPDATES="${PUBLISH_LOFTY_PM_UPDATES:-1}"
export APPLY_LOFTY_GUARDED_UPDATES="${APPLY_LOFTY_GUARDED_UPDATES:-1}"
export SEND_MONTHLY_DISCORD_PROPERTY_UPDATE="${SEND_MONTHLY_DISCORD_PROPERTY_UPDATE:-1}"
export SEND_OWNER_EMAILS="${SEND_OWNER_EMAILS:-1}"
export SEND_NATIVE_LOFTY_OWNER_EMAILS="${SEND_NATIVE_LOFTY_OWNER_EMAILS:-1}"
export SEND_TRANSFER_RECONCILIATION_TELEGRAM="${SEND_TRANSFER_RECONCILIATION_TELEGRAM:-1}"
export YHOME_GSHEET_APPLY="${YHOME_GSHEET_APPLY:-1}"
export YHOME_GSHEET_WRITE_ENABLED="${YHOME_GSHEET_WRITE_ENABLED:-1}"
RUN_MONTH="${RUN_MONTH:-$($PY - "$MONTHLY_RUN_REPORT" <<'PY'
import json
import sys
from datetime import date
from pathlib import Path

today = date.today()
year = today.year
month = today.month - 1
if month == 0:
    year -= 1
    month = 12
fallback = f"{year:04d}-{month:02d}"
try:
    report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, ValueError, TypeError):
    report = {}
candidate = str(report.get("run_month") or "").strip()
status = str(report.get("effective_status") or report.get("status") or "").strip()
if status in {"failed", "review"} and len(candidate) == 7 and candidate[4] == "-":
    print(candidate)
else:
    print(fallback)
PY
)}"

mkdir -p "$REPORT_DIR"

write_report() {
  local status="$1"
  local reason="$2"
  local eligible="$3"
  local recovery_rc="${4:-}"
  "$PY" - "$RECOVERY_REPORT" "$RUN_MONTH" "$CURRENT_LOCAL_DATE" "$CURRENT_LOCAL_DAY" "$status" "$reason" "$eligible" "$recovery_rc" "$MONTHLY_RUN_REPORT" "$MONTHLY_TRUTH_REPORT" "$LATE_CLOSE_APPROVED" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

(
    report_path,
    run_month,
    current_local_date,
    current_local_day,
    status,
    reason,
    eligible,
    recovery_rc,
    monthly_run_report,
    monthly_truth_report,
    late_close_approved,
) = sys.argv[1:]

payload = {
    "job": "baselane-financials-monthly-recovery-cron",
    "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "status": status,
    "run_month": run_month,
    "current_local_date": current_local_date,
    "current_local_day": int(current_local_day),
    "eligible": eligible == "1",
    "reason": reason,
    "recovery_return_code": int(recovery_rc) if recovery_rc else None,
    "monthly_run_report": monthly_run_report,
    "monthly_truth_report": monthly_truth_report,
    "late_close_approval_requested": late_close_approved == "1",
    "off_cycle_policy": "An auth-blocked prior-month run may recover automatically. A non-auth late close requires BASELANE_MONTHLY_LATE_CLOSE_APPROVED=1 for the immediately preceding month; the monthly cron retains all source, financial, listing, Discord, and email guards.",
    "no_spam_policy": "A recovery run is not eligible once the monthly owner-email sent-state records the run month.",
}
Path(report_path).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY
}

eligibility="$($PY - "$MONTHLY_RUN_REPORT" "$MONTHLY_TRUTH_REPORT" "$SENT_STATE_FILE" "$RUN_MONTH" "$CURRENT_LOCAL_DAY" "$CURRENT_LOCAL_DATE" "$LATE_CLOSE_APPROVED" <<'PY'
import json
import sys
from datetime import date
from pathlib import Path

(
    monthly_report_path,
    truth_report_path,
    sent_state_path,
    run_month,
    current_day,
    current_local_date,
    late_close_approved,
) = sys.argv[1:]
current_day = int(current_day)
late_close_approved = late_close_approved == "1"

def read_json(path: str) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}

monthly = read_json(monthly_report_path)
truth = read_json(truth_report_path)

embedded_auth_blocked = (
    monthly.get("monthly_finance_truth_refresh_auth_blocked") is True
    and monthly.get("monthly_finance_truth_refresh_safe_to_retry_after_manual_auth") is True
)
auth_blocked = truth.get("auth_blocked") is True or embedded_auth_blocked
auth_failure_shape = (
    monthly.get("effective_failed_step") in {
        "baselane_monthly_finance_truth_refresh",
        "baselane_monthly_statements_idempotent",
    }
    or embedded_auth_blocked
)
try:
    current_date = date.fromisoformat(current_local_date)
    prior_month = current_date.replace(day=1)
    prior_month = prior_month.replace(
        year=prior_month.year - 1 if prior_month.month == 1 else prior_month.year,
        month=12 if prior_month.month == 1 else prior_month.month - 1,
    ).strftime("%Y-%m")
except ValueError:
    prior_month = ""

if current_day < 16:
    print("0\tnot_after_protected_15th_window")
elif monthly.get("run_month") != run_month:
    print("0\tmonthly_run_month_mismatch")
elif monthly.get("effective_status") not in {"failed", "review"}:
    print("0\tno_recoverable_monthly_failure")
elif Path(sent_state_path).is_file() and Path(sent_state_path).read_text(encoding="utf-8").strip() == run_month:
    print("0\towner_email_already_marked_sent_for_run_month")
elif monthly.get("lofty_pm_publish_publish_attempted") is True:
    print("0\tlofty_publish_already_attempted")
elif auth_failure_shape and auth_blocked and truth.get("downstream_generation_allowed") is not True and monthly.get("monthly_finance_truth_refresh_downstream_generation_allowed") is not True:
    print("1\tauth_blocked_monthly_close_requires_guarded_recovery")
elif late_close_approved and run_month == prior_month and truth.get("auth_blocked") is not True and truth.get("status") in {"ok", "ok_dry_run"}:
    print("1\texplicitly_approved_late_current_month_close")
elif monthly.get("effective_status") == "failed" and monthly.get("lofty_pm_publish_publish_attempted") is not True:
    print("1\tfailed_prepublication_monthly_close_requires_idempotent_recovery")
elif auth_failure_shape:
    print("0\tfinance_truth_failure_was_not_auth_blocked")
else:
    print("0\tno_auth_blocked_monthly_failure")
PY
)"
IFS=$'\t' read -r eligible reason <<< "$eligibility"

if [ "$eligible" != "1" ]; then
  write_report "not_eligible" "$reason" "0"
  exit 0
fi

if [ "${BASELANE_MONTHLY_RECOVERY_CHECK_ONLY:-0}" = "1" ]; then
  write_report "eligible_check_only" "$reason" "1"
  exit 0
fi

override_digest="$($PY - "$RUN_MONTH" "$CURRENT_LOCAL_DATE" <<'PY'
import hashlib
import json
import sys

payload = {
    "action": "allow_off_cycle_monthly_live_send",
    "run_month": sys.argv[1],
    "current_local_date": sys.argv[2],
    "required_day": "15",
}
print(hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest())
PY
)"

write_report "started" "$reason" "1"
set +e
flock -n -E 75 "$LOCK_PATH" timeout --kill-after=30s "${BASELANE_MONTHLY_RECOVERY_TIMEOUT:-2h}" env \
  RUN_MONTH="$RUN_MONTH" \
  CURRENT_LOCAL_DATE="$CURRENT_LOCAL_DATE" \
  CURRENT_LOCAL_DAY="$CURRENT_LOCAL_DAY" \
  ALLOW_OFF_CYCLE_MONTHLY_LIVE_SEND=1 \
  ALLOW_OFF_CYCLE_MONTHLY_LIVE_SEND_DIGEST="$override_digest" \
  bash "$ROOT/scripts/baselane_financials_monthly_cron.sh"
recovery_rc="$?"
set -e

if [ "$recovery_rc" -eq 0 ]; then
  write_report "ok" "$reason" "1" "$recovery_rc"
else
  write_report "review" "$reason" "1" "$recovery_rc"
fi
exit "$recovery_rc"
