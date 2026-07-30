#!/usr/bin/env bash
# Monthly accruals idempotent hook for Baselane daily sync
# Generates and applies missing monthly accruals (insurance, legal, taxes, PM fees)
# for the previous month. Idempotent: skips accruals already present in the GL.
#
# Environment:
#   WORKSPACE_ROOT - workspace root (defaults to script parent dir)
#   BASELANE_LEDGER_PATH - path to ECO Systems General Ledger.csv
#   BASELANE_MONTHLY_ACCRUALS_MONTH - target accrual month YYYY-MM (defaults to previous month)
#   BASELANE_MONTHLY_ACCRUALS_DRY_RUN - set to 1 for dry-run only
#   BASELANE_MONTHLY_ACCRUALS_APPLY - set to 1 to append to the local GL CSV (default: 0)
#   BASELANE_MONTHLY_ACCRUALS_ACTIVE_PROPERTY_MAP - active property map requiring accrual template coverage
#   BASELANE_PM_RATE_SCHEDULE - bookkeeping workbook containing per-property PM fee percentages
#   BASELANE_LOFTY_RESERVE_SNAPSHOT - fresh get-manager-properties response used by the co-ownership OR policy
#   BASELANE_MONTHLY_ACCRUALS_IMPORT_GAP_APPROVAL_CSV - set to 1 to import reviewed gap approval CSV before validation

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${WORKSPACE_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
PY="${BASELANE_WORKBOOK_PYTHON:-${PYTHON_BIN:-python3}}"
if ! "$PY" -c 'import openpyxl' >/dev/null 2>&1; then
  for candidate in /home/linuxbrew/.linuxbrew/bin/python3 /home/linuxbrew/.linuxbrew/opt/python@3.14/bin/python3.14; do
    if [ -x "$candidate" ] && "$candidate" -c 'import openpyxl' >/dev/null 2>&1; then
      PY="$candidate"
      break
    fi
  done
fi
if ! "$PY" -c 'import openpyxl' >/dev/null 2>&1; then
  echo "[baselane-monthly-accruals] no Python interpreter with openpyxl is available" >&2
  exit 1
fi

# Determine previous month for accrual target
PREV_YEAR="$(date -d '1 month ago' +%Y 2>/dev/null || date -v-1m +%Y)"
PREV_MONTH="$(date -d '1 month ago' +%m 2>/dev/null || date -v-1m +%m)"

# Fallback for systems without date -d
if [ -z "$PREV_YEAR" ] || [ -z "$PREV_MONTH" ]; then
  PREV_YEAR="$($PY -c 'import datetime; d = datetime.date.today().replace(day=1); prev = (d - datetime.timedelta(days=1)).replace(day=1); print(prev.year)')"
  PREV_MONTH="$($PY -c 'import datetime; d = datetime.date.today().replace(day=1); prev = (d - datetime.timedelta(days=1)).replace(day=1); print(f"{prev.month:02d}")')"
fi
TARGET_MONTH="${BASELANE_MONTHLY_ACCRUALS_MONTH:-${PREV_YEAR}-${PREV_MONTH}}"
PREV_YEAR="${TARGET_MONTH%-*}"
PREV_MONTH="${TARGET_MONTH#*-}"

# Resolve ledger path
if [ -z "${BASELANE_LEDGER_PATH:-}" ]; then
  if [ -d /mnt/c/Users/digit/Dropbox/Projects/assetrail ]; then
    BASELANE_LEDGER_PATH="/mnt/c/Users/digit/Dropbox/Projects/assetrail/ECO Systems General Ledger.csv"
  elif [ -d /data/Dropbox/Projects/assetrail ]; then
    BASELANE_LEDGER_PATH="/data/Dropbox/Projects/assetrail/ECO Systems General Ledger.csv"
  elif [ -d "$HOME/Dropbox/Projects/assetrail" ]; then
    BASELANE_LEDGER_PATH="$HOME/Dropbox/Projects/assetrail/ECO Systems General Ledger.csv"
  else
    echo "[baselane-monthly-accruals] BASELANE_LEDGER_PATH not set and default path not found" >&2
    exit 1
  fi
fi

if [ ! -f "$BASELANE_LEDGER_PATH" ]; then
  echo "[baselane-monthly-accruals] Ledger not found: $BASELANE_LEDGER_PATH" >&2
  exit 1
fi

REPORT_DIR="${BASELANE_REPORT_DIR:-$ROOT/reports}"
mkdir -p "$REPORT_DIR"
REPORT_FILE="$REPORT_DIR/baselane_monthly_accruals_${PREV_YEAR}${PREV_MONTH}.json"
REVIEW_MARKDOWN_FILE="${BASELANE_MONTHLY_ACCRUALS_REVIEW_MARKDOWN:-$REPORT_DIR/baselane_monthly_accruals_${PREV_YEAR}${PREV_MONTH}_review.md}"

ACCRUAL_SCRIPT="$SCRIPT_DIR/baselane_monthly_accruals_idempotent.py"
if [ ! -x "$ACCRUAL_SCRIPT" ]; then
  echo "[baselane-monthly-accruals] Accrual script not found: $ACCRUAL_SCRIPT" >&2
  exit 1
fi

DRY_RUN="${BASELANE_MONTHLY_ACCRUALS_DRY_RUN:-0}"
APPLY="${BASELANE_MONTHLY_ACCRUALS_APPLY:-0}"

ACCRUAL_ARGS=(
  --gl-csv "$BASELANE_LEDGER_PATH"
  --month "${PREV_YEAR}-${PREV_MONTH}"
  --report "$REPORT_FILE"
  --review-markdown "$REVIEW_MARKDOWN_FILE"
  --gap-approval-scaffold "${BASELANE_MONTHLY_ACCRUALS_GAP_APPROVAL_SCAFFOLD:-$ROOT/config/baselane_monthly_accrual_gap_approvals.json}"
  --gap-approval-review-csv "${BASELANE_MONTHLY_ACCRUALS_GAP_APPROVAL_REVIEW_CSV:-$REPORT_DIR/baselane_monthly_accrual_gap_approvals_review.csv}"
  --gap-approval-import-commands "${BASELANE_MONTHLY_ACCRUALS_GAP_APPROVAL_IMPORT_COMMANDS:-$REPORT_DIR/baselane_monthly_accrual_gap_approvals_import.requires-explicit-approval.sh}"
  --update-amount-mismatches
)

HEMLANE_LIVE_TRANSACTIONS="${BASELANE_HEMLANE_LIVE_TRANSACTIONS_REPORT:-$REPORT_DIR/hemlane_live_transactions.json}"
if [ -f "$HEMLANE_LIVE_TRANSACTIONS" ]; then
  ACCRUAL_ARGS+=(--hemlane-live-transactions "$HEMLANE_LIVE_TRANSACTIONS")
fi

LOFTY_RESERVE_SNAPSHOT="${BASELANE_LOFTY_RESERVE_SNAPSHOT:-$ROOT/reports/lofty-pm-current/get-manager-properties.full-response.json}"
if [ ! -f "$LOFTY_RESERVE_SNAPSHOT" ]; then
  echo "[baselane-monthly-accruals] Lofty reserve snapshot not found: $LOFTY_RESERVE_SNAPSHOT" >&2
  exit 1
fi
ACCRUAL_ARGS+=(--lofty-reserve-snapshot "$LOFTY_RESERVE_SNAPSHOT")

PM_RATE_SCHEDULE="${BASELANE_PM_RATE_SCHEDULE:-}"
if [ -z "$PM_RATE_SCHEDULE" ]; then
  for candidate in \
    "/mnt/c/Users/digit/Dropbox/Real Estate/Lofty PM/Earl Co - Schedule of Real Estate Owned.xlsx" \
    "/mnt/c/Users/digit/Dropbox/Real Estate/Lofty PM/Earl Co - Property Management Portfolio (Bookkeeping).xlsx" \
    "/data/Dropbox/Real Estate/Lofty PM/Earl Co - Schedule of Real Estate Owned.xlsx" \
    "/data/Dropbox/Real Estate/Lofty PM/Earl Co - Property Management Portfolio (Bookkeeping).xlsx"; do
    if [ -f "$candidate" ]; then
      PM_RATE_SCHEDULE="$candidate"
      break
    fi
  done
fi
if [ -z "$PM_RATE_SCHEDULE" ] || [ ! -f "$PM_RATE_SCHEDULE" ]; then
  echo "[baselane-monthly-accruals] PM rate schedule not found; refusing stale hard-coded percentages" >&2
  exit 1
fi
ACCRUAL_ARGS+=(--pm-rate-schedule "$PM_RATE_SCHEDULE")

ACTIVE_PROPERTY_MAP="${BASELANE_MONTHLY_ACCRUALS_ACTIVE_PROPERTY_MAP:-$ROOT/skills/lofty-pm/config/property_update_map.json}"
if [ -f "$ACTIVE_PROPERTY_MAP" ]; then
  ACCRUAL_ARGS+=(--active-property-map "$ACTIVE_PROPERTY_MAP")
fi

GAP_APPROVALS="${BASELANE_MONTHLY_ACCRUALS_GAP_APPROVALS:-$ROOT/config/baselane_monthly_accrual_gap_approvals.json}"
if [ -f "$GAP_APPROVALS" ]; then
  ACCRUAL_ARGS+=(--gap-approvals "$GAP_APPROVALS")
fi

if [ "${BASELANE_MONTHLY_ACCRUALS_IMPORT_GAP_APPROVAL_CSV:-0}" = "1" ]; then
  ACCRUAL_ARGS+=(--import-gap-approval-csv)
fi

if [ "$DRY_RUN" = "1" ]; then
  :
elif [ "$APPLY" = "1" ]; then
  ACCRUAL_ARGS+=(--apply)
fi

echo "[baselane-monthly-accruals] Running monthly accruals for ${PREV_YEAR}-${PREV_MONTH}"
"$PY" "$ACCRUAL_SCRIPT" "${ACCRUAL_ARGS[@]}"

OBIE_CASH_BASIS_CLEANUP_SCRIPT="$SCRIPT_DIR/obie_cash_basis_insurance_cleanup.py"
if [ ! -x "$OBIE_CASH_BASIS_CLEANUP_SCRIPT" ]; then
  echo "[baselane-monthly-accruals] Obie cash-basis insurance cleanup script not found: $OBIE_CASH_BASIS_CLEANUP_SCRIPT" >&2
  exit 1
fi
OBIE_SOURCE_INDEX="${BASELANE_SOURCE_TRANSACTION_INDEX:-$ROOT/reports/baselane_source_transaction_index.csv}"
OBIE_CLEANUP_ARGS=(
  --gl-csv "$BASELANE_LEDGER_PATH"
  --source-index "$OBIE_SOURCE_INDEX"
  --report-json "${BASELANE_OBIE_INSURANCE_CLEANUP_REPORT_JSON:-$REPORT_DIR/obie_cash_basis_insurance_cleanup.json}"
  --report-md "${BASELANE_OBIE_INSURANCE_CLEANUP_REPORT_MD:-/mnt/c/Users/digit/Dropbox/Real Estate/Lofty PM/Obie/OH-IL-TN Cash-Basis Insurance Duplicate Audit.md}"
  --report-csv "${BASELANE_OBIE_INSURANCE_CLEANUP_REPORT_CSV:-/mnt/c/Users/digit/Dropbox/Real Estate/Lofty PM/Obie/OH-IL-TN Cash-Basis Insurance Duplicate Audit.csv}"
)
if [ "$DRY_RUN" != "1" ] && [ "$APPLY" = "1" ]; then
  OBIE_CLEANUP_ARGS+=(--apply-local)
fi
if [ "${BASELANE_OBIE_INSURANCE_CLEANUP_APPLY_LIVE:-0}" = "1" ]; then
  OBIE_CLEANUP_ARGS+=(--apply-live)
fi
echo "[baselane-monthly-accruals] Auditing OH/IL/TN cash-basis insurance accrual cleanup"
"$PY" "$OBIE_CASH_BASIS_CLEANUP_SCRIPT" "${OBIE_CLEANUP_ARGS[@]}"

# The first report describes the pre-apply plan. Regenerate it after all local
# writes so the scheduler gates on the actual ledger state.
if [ "$DRY_RUN" != "1" ] && [ "$APPLY" = "1" ]; then
  POST_APPLY_ACCRUAL_ARGS=()
  for arg in "${ACCRUAL_ARGS[@]}"; do
    if [ "$arg" != "--apply" ]; then
      POST_APPLY_ACCRUAL_ARGS+=("$arg")
    fi
  done
  echo "[baselane-monthly-accruals] Verifying post-apply accrual state"
  "$PY" "$ACCRUAL_SCRIPT" "${POST_APPLY_ACCRUAL_ARGS[@]}"
fi

"$PY" - "$REPORT_FILE" <<'PY'
import json
import sys
from pathlib import Path

report_path = Path(sys.argv[1])
if not report_path.is_file():
    print(f"[baselane-monthly-accruals] report missing after run: {report_path}", file=sys.stderr)
    sys.exit(1)

data = json.loads(report_path.read_text(encoding="utf-8"))
status = str(data.get("status") or "").strip().lower()
blocker_fields = {
    "amount_mismatch_count": int(data.get("amount_mismatch_count") or 0),
    "blocked_first_day_pm_fee_count": int(data.get("blocked_first_day_pm_fee_count") or 0),
    "blocking_gap_action_count": int(data.get("blocking_gap_action_count") or 0),
    "unapproved_pm_fee_basis_gap_count": int(
        data.get("unapproved_pm_fee_basis_gap_count")
        if data.get("unapproved_pm_fee_basis_gap_count") is not None
        else data.get("pm_fee_basis_gap_count") or 0
    ),
}
blockers = [f"{name}={value}" for name, value in blocker_fields.items() if value]
if status == "ok" and not blockers:
    print(f"[baselane-monthly-accruals] Monthly accruals verified ok: {report_path}")
    sys.exit(0)

queue = data.get("gap_action_queue") if isinstance(data.get("gap_action_queue"), list) else []
first = queue[0] if queue and isinstance(queue[0], dict) else {}
detail = ""
if first:
    detail = (
        f"; first_blocker={first.get('property') or 'unknown'} "
        f"{first.get('action') or first.get('reason') or 'review_required'}"
    )
print(
    "[baselane-monthly-accruals] Monthly accruals require review: "
    f"status={status or 'missing'} "
    f"{' '.join(blockers) if blockers else 'no_counted_blockers'}"
    f"{detail}; report={report_path}",
    file=sys.stderr,
)
sys.exit(2)
PY
