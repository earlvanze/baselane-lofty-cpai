#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${WORKSPACE_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
PY="${PYTHON_BIN:-python3}"
case "$PY" in
  "/usr/bin/env python3"|"env python3") PY="python3" ;;
esac

export ROOT WORKSPACE_ROOT="$ROOT"

REPORT="$ROOT/reports/baselane_hemlane_auto_tag_report.json"
FILTERED_APPROVED="$ROOT/reports/baselane_hemlane_auto_tag_approved_corrections.csv"
APPLY_PLAN_JSON="$ROOT/reports/baselane_hemlane_auto_tag_apply_plan.json"
APPLY_PLAN_CSV="$ROOT/reports/baselane_hemlane_auto_tag_apply_plan.csv"
APPLY_PLAN_MD="$ROOT/reports/baselane_hemlane_auto_tag_apply_plan.md"
APPLY_JSON="$ROOT/reports/baselane_hemlane_auto_tag_apply.json"
APPLY_CSV="$ROOT/reports/baselane_hemlane_auto_tag_apply.csv"
APPLY_MD="$ROOT/reports/baselane_hemlane_auto_tag_apply.md"
APPLY_PAYLOAD="$ROOT/reports/baselane_hemlane_auto_tag_apply_payload.json"
LIVE_APPLY_GUARD="$ROOT/reports/baselane_hemlane_auto_tag_live_apply_guard.json"

write_report() {
  local status="$1"
  local reason="${2:-}"
  mkdir -p "$ROOT/reports"
  BASELANE_HEMLANE_AUTOTAG_STATUS="$status" \
  BASELANE_HEMLANE_AUTOTAG_REASON="$reason" \
  BASELANE_HEMLANE_AUTOTAG_FILTERED_APPROVED="$FILTERED_APPROVED" \
  BASELANE_HEMLANE_AUTOTAG_APPLY_PLAN="$APPLY_PLAN_JSON" \
  BASELANE_HEMLANE_AUTOTAG_APPLY="$APPLY_JSON" \
  BASELANE_HEMLANE_AUTOTAG_LIVE_APPLY_GUARD="$LIVE_APPLY_GUARD" \
  "$PY" - <<'PY'
import json
import os
from datetime import datetime, timezone
from pathlib import Path

root = Path(os.environ["ROOT"])
report_path = root / "reports" / "baselane_hemlane_auto_tag_report.json"
apply_path = Path(os.environ["BASELANE_HEMLANE_AUTOTAG_APPLY"])
plan_path = Path(os.environ["BASELANE_HEMLANE_AUTOTAG_APPLY_PLAN"])
filtered_path = Path(os.environ["BASELANE_HEMLANE_AUTOTAG_FILTERED_APPROVED"])
live_apply_guard_path = Path(os.environ["BASELANE_HEMLANE_AUTOTAG_LIVE_APPLY_GUARD"])

def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

apply_report = read_json(apply_path)
plan_report = read_json(plan_path)
live_apply_guard = read_json(live_apply_guard_path)
report = {
    "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "status": os.environ["BASELANE_HEMLANE_AUTOTAG_STATUS"],
    "reason": os.environ.get("BASELANE_HEMLANE_AUTOTAG_REASON") or None,
    "policy": "Daily auto-tagging is scoped to live Hemlane transaction evidence only: exactly one matched completed Hemlane rent transaction, category Rents, current Baselane source-index ID, and existing source-fix apply preflight.",
    "filtered_approved_corrections_csv": str(filtered_path),
    "filtered_approved_count": sum(1 for _ in filtered_path.open(encoding="utf-8", errors="ignore")) - 1 if filtered_path.is_file() else 0,
    "apply_plan_report": str(plan_path),
    "apply_plan_status": plan_report.get("status"),
    "apply_plan_ready_current_source_index_count": plan_report.get("ready_current_source_index_count"),
    "apply_plan_blocked_count": plan_report.get("blocked_count"),
    "apply_report": str(apply_path),
    "live_apply_guard_report": str(live_apply_guard_path),
    "live_apply_guard": live_apply_guard,
    "apply_status": apply_report.get("status"),
    "apply_mode": apply_report.get("mode"),
    "ready_to_apply_count": apply_report.get("ready_to_apply_count"),
    "already_applied_count": apply_report.get("already_applied_count"),
    "applied_count": apply_report.get("applied_count"),
    "failed_count": apply_report.get("failed_count"),
    "blocked_count": apply_report.get("blocked_count"),
    "apply_preflight": apply_report.get("apply_preflight"),
}
report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps({k: report.get(k) for k in ("status", "filtered_approved_count", "applied_count", "already_applied_count")}, indent=2, sort_keys=True))
PY
}

if [ "${BASELANE_HEMLANE_AUTO_TAG_ENABLED:-1}" != "1" ]; then
  write_report "skipped_by_env" "BASELANE_HEMLANE_AUTO_TAG_ENABLED!=1"
  exit 0
fi

mkdir -p "$ROOT/reports"

set +e
"$PY" "$ROOT/scripts/baselane_ecogl_source_fix_plan.py" \
  --root "$ROOT" \
  --report "$ROOT/reports/baselane_ecogl_source_fix_plan.json" \
  --actions-csv "$ROOT/reports/baselane_ecogl_source_fix_actions.csv" \
  --markdown "$ROOT/reports/baselane_ecogl_source_fix_plan.md"
plan_rc="$?"

timeout --kill-after=15s "${ECOGL_SOURCE_FIX_EVIDENCE_TIMEOUT_SECONDS:-360}s" "$PY" "$ROOT/scripts/baselane_ecogl_source_fix_evidence.py" \
  --root "$ROOT" \
  --actions-csv "$ROOT/reports/baselane_ecogl_source_fix_actions.csv" \
  --source-plan "$ROOT/reports/baselane_ecogl_source_fix_plan.json" \
  --report "$ROOT/reports/baselane_ecogl_source_fix_evidence.json" \
  --markdown "$ROOT/reports/baselane_ecogl_source_fix_evidence.md"
evidence_rc="$?"

"$PY" "$ROOT/scripts/baselane_ecogl_source_fix_verifier.py" \
  --root "$ROOT" \
  --actions-csv "$ROOT/reports/baselane_ecogl_source_fix_actions.csv" \
  --report "$ROOT/reports/baselane_ecogl_source_fix_verifier.json" \
  --markdown "$ROOT/reports/baselane_ecogl_source_fix_verifier.md"
verifier_rc="$?"

"$PY" "$ROOT/scripts/baselane_ecogl_source_fix_corrections.py" \
  --root "$ROOT" \
  --evidence "$ROOT/reports/baselane_ecogl_source_fix_evidence.json" \
  --verifier "$ROOT/reports/baselane_ecogl_source_fix_verifier.json" \
  --report "$ROOT/reports/baselane_ecogl_source_fix_corrections.json" \
  --csv "$ROOT/reports/baselane_ecogl_source_fix_corrections.csv" \
  --markdown "$ROOT/reports/baselane_ecogl_source_fix_corrections.md"
corrections_rc="$?"

"$PY" "$ROOT/scripts/baselane_ecogl_source_fix_approval.py" \
  --root "$ROOT" \
  --corrections-report "$ROOT/reports/baselane_ecogl_source_fix_corrections.json" \
  --corrections-csv "$ROOT/reports/baselane_ecogl_source_fix_corrections.csv" \
  --approval "$ROOT/reports/baselane_ecogl_source_fix_approval.json" \
  --approved-csv "$ROOT/reports/baselane_ecogl_source_fix_approved_corrections.csv" \
  --markdown "$ROOT/reports/baselane_ecogl_source_fix_approval.md"
approval_rc="$?"

"$PY" "$ROOT/scripts/baselane_ecogl_source_fix_correction_validator.py" \
  --root "$ROOT" \
  --corrections-csv "$ROOT/reports/baselane_ecogl_source_fix_approved_corrections.csv" \
  --corrections-report "$ROOT/reports/baselane_ecogl_source_fix_approval.json" \
  --report "$ROOT/reports/baselane_ecogl_source_fix_correction_validation.json" \
  --csv "$ROOT/reports/baselane_ecogl_source_fix_correction_validation.csv" \
  --markdown "$ROOT/reports/baselane_ecogl_source_fix_correction_validation.md"
validation_rc="$?"
set -e

"$PY" - <<'PY'
import csv
import os
from pathlib import Path

root = Path(os.environ["ROOT"])
source = root / "reports" / "baselane_ecogl_source_fix_approved_corrections.csv"
target = root / "reports" / "baselane_hemlane_auto_tag_approved_corrections.csv"
rows = []
with source.open(newline="", encoding="utf-8-sig") as handle:
    reader = csv.DictReader(handle)
    fieldnames = list(reader.fieldnames or [])
    for row in reader:
        context = str(row.get("context_candidate_status") or "").strip()
        category = str(row.get("operator_category_to_set_in_baselane") or "").strip()
        merchant_description = f"{row.get('merchant') or ''} {row.get('description') or ''}".lower()
        if context != "automation_safe_hemlane_live_transaction":
            continue
        if category != "Rents":
            continue
        if "hemlane" not in merchant_description:
            continue
        rows.append(row)
target.parent.mkdir(parents=True, exist_ok=True)
with target.open("w", newline="", encoding="utf-8") as handle:
    writer = csv.DictWriter(handle, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
PY

set +e
"$PY" "$ROOT/scripts/baselane_ecogl_source_fix_apply_plan.py" \
  --root "$ROOT" \
  --approved-csv "$FILTERED_APPROVED" \
  --validation-report "$ROOT/reports/baselane_ecogl_source_fix_correction_validation.json" \
  --report "$APPLY_PLAN_JSON" \
  --csv "$APPLY_PLAN_CSV" \
  --markdown "$APPLY_PLAN_MD"
apply_plan_rc="$?"
set -e

HEMLANE_APPLY_PLAN_SUMMARY="$("$PY" - "$APPLY_PLAN_JSON" <<'PY'
import json
import sys
from pathlib import Path

try:
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except Exception:
    payload = {}
print(
    "\t".join(
        [
            str(payload.get("status") or ""),
            str(int(payload.get("ready_current_source_index_count") or 0)),
            str(int(payload.get("blocked_count") or 0)),
        ]
    )
)
PY
)"
IFS=$'\t' read -r HEMLANE_APPLY_PLAN_STATUS HEMLANE_APPLY_PLAN_READY_COUNT HEMLANE_APPLY_PLAN_BLOCKED_COUNT <<<"$HEMLANE_APPLY_PLAN_SUMMARY"
if [ "$apply_plan_rc" -ne 0 ]; then
  write_report "review" "hemlane_apply_plan_rc_${apply_plan_rc}"
  exit "$apply_plan_rc"
fi
if [ "$HEMLANE_APPLY_PLAN_STATUS" = "ok" ] && [ "${HEMLANE_APPLY_PLAN_READY_COUNT:-0}" = "0" ] && [ "${HEMLANE_APPLY_PLAN_BLOCKED_COUNT:-0}" = "0" ]; then
  "$PY" "$ROOT/scripts/baselane_ecogl_source_fix_apply.py" \
    --root "$ROOT" \
    --apply-plan-csv "$APPLY_PLAN_CSV" \
    --source-index-csv "$ROOT/reports/baselane_source_transaction_index.csv" \
    --report "$APPLY_JSON" \
    --csv "$APPLY_CSV" \
    --markdown "$APPLY_MD" \
    --payload "$APPLY_PAYLOAD" >/dev/null
  write_report "ok" "no_hemlane_autotag_candidates"
  exit 0
fi

"$PY" "$ROOT/scripts/baselane_daily_sync_report.py" \
  --root "$ROOT" \
  --report "$ROOT/reports/baselane_daily_sync_report.json" >/dev/null || true

LIVE_APPLY_GUARD_STATUS="not_required"
if [ "${BASELANE_HEMLANE_AUTO_TAG_APPLY:-1}" = "1" ]; then
  set +e
  "$PY" - "$ROOT" "$APPLY_PLAN_JSON" "$LIVE_APPLY_GUARD" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

root = Path(sys.argv[1])
apply_plan_path = Path(sys.argv[2])
guard_path = Path(sys.argv[3])

def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "missing" if not path.exists() else "unreadable", "error": str(exc)}

daily = read_json(root / "reports" / "baselane_daily_sync_report.json")
apply_plan = read_json(apply_plan_path)
export_guard = read_json(root / "reports" / "baselane_export_guard_last.json")
issues = []
daily_status = daily.get("status")
daily_sync_clean_for_hemlane = (
    daily_status == "ok"
    or (
        daily_status == "review"
        and daily.get("sync_report_status") == "ok"
        and daily.get("sync_status") == "ok"
        and daily.get("failed_step") == "baselane_hemlane_auto_tag_source_fix"
        and int(daily.get("split_output_mismatch_count") or 0) == 0
        and int(daily.get("split_unresolved_property_count") or 0) == 0
        and int(daily.get("source_cash_balance_violation_count") or 0) == 0
    )
)
if not daily_sync_clean_for_hemlane:
    issues.append(f"daily_sync={daily.get('status') or 'missing'}")
if apply_plan.get("status") != "ok":
    issues.append(f"apply_plan={apply_plan.get('status') or 'missing'}")
if int(apply_plan.get("needs_current_source_index_refresh_count") or 0) != 0:
    issues.append(f"needs_current_source_index_refresh_count={apply_plan.get('needs_current_source_index_refresh_count')}")
if int(apply_plan.get("blocked_count") or 0) != 0:
    issues.append(f"apply_plan_blocked_count={apply_plan.get('blocked_count')}")
if str(export_guard.get("source_transaction_index_current_write_status") or "written_current") != "written_current":
    issues.append(f"source_transaction_index_current_write_status={export_guard.get('source_transaction_index_current_write_status')}")

guard = {
    "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "status": "ok" if not issues else "review",
    "issue_count": len(issues),
    "issues": issues,
    "policy": "Standalone Hemlane auto-tag live apply requires clean daily sync, current source index, and an unblocked current-source apply plan; otherwise wrapper must run dry-run only.",
    "daily_sync_status": daily_status,
    "daily_sync_clean_for_hemlane": daily_sync_clean_for_hemlane,
    "daily_failed_step": daily.get("failed_step"),
    "daily_sync_status_for_hemlane_reason": "hemlane_auto_tag_self_review" if daily_status == "review" and daily_sync_clean_for_hemlane else None,
    "apply_plan_status": apply_plan.get("status"),
    "ready_current_source_index_count": apply_plan.get("ready_current_source_index_count"),
    "needs_current_source_index_refresh_count": apply_plan.get("needs_current_source_index_refresh_count"),
    "apply_plan_blocked_count": apply_plan.get("blocked_count"),
    "source_transaction_index_current_write_status": export_guard.get("source_transaction_index_current_write_status"),
}
guard_path.parent.mkdir(parents=True, exist_ok=True)
guard_path.write_text(json.dumps(guard, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(guard["status"])
raise SystemExit(0 if guard["status"] == "ok" else 2)
PY
  live_apply_guard_rc="$?"
  set -e
  if [ "$live_apply_guard_rc" -eq 0 ]; then
    LIVE_APPLY_GUARD_STATUS="ok"
  else
    LIVE_APPLY_GUARD_STATUS="review"
    echo "[baselane] Hemlane auto-tag live apply guard blocked mutation; running dry-run only" >&2
  fi
fi

set +e
if [ "${BASELANE_HEMLANE_AUTO_TAG_APPLY:-1}" = "1" ] && [ "$LIVE_APPLY_GUARD_STATUS" = "ok" ]; then
  BASELANE_SOURCE_FIX_APPLY=1 "$PY" "$ROOT/scripts/baselane_ecogl_source_fix_apply.py" \
    --root "$ROOT" \
    --apply-plan-csv "$APPLY_PLAN_CSV" \
    --source-index-csv "$ROOT/reports/baselane_source_transaction_index.csv" \
    --report "$APPLY_JSON" \
    --csv "$APPLY_CSV" \
    --markdown "$APPLY_MD" \
    --payload "$APPLY_PAYLOAD" \
    --apply
else
  "$PY" "$ROOT/scripts/baselane_ecogl_source_fix_apply.py" \
    --root "$ROOT" \
    --apply-plan-csv "$APPLY_PLAN_CSV" \
    --source-index-csv "$ROOT/reports/baselane_source_transaction_index.csv" \
    --report "$APPLY_JSON" \
    --csv "$APPLY_CSV" \
    --markdown "$APPLY_MD" \
    --payload "$APPLY_PAYLOAD"
fi
apply_rc="$?"
set -e
if [ "$apply_rc" -ne 0 ]; then
  write_report "review" "hemlane_apply_rc_${apply_rc}"
  exit "$apply_rc"
fi
if [ "$LIVE_APPLY_GUARD_STATUS" = "review" ]; then
  write_report "review" "live_apply_guard_blocked"
  exit 2
fi

write_report "ok" ""
