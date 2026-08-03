#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${WORKSPACE_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
CAPTURE_HAR_PATH="${CITADEL_CAPTURE_HAR_PATH:-$ROOT/reports/citadel_replay_capture.har}"
CAPTURE_REPORT="${CITADEL_CAPTURE_REPORT:-$ROOT/reports/citadel_replay_capture_report.json}"
CAPTURE_DIAGNOSTICS_REPORT="${CITADEL_CAPTURE_DIAGNOSTICS_REPORT:-$ROOT/reports/citadel_replay_capture_diagnostics_report.json}"
CAPTURE_PLAN_REPORT="${CITADEL_CAPTURE_DIAGNOSTICS_PLAN_REPORT:-$ROOT/reports/citadel_replay_capture_plan.json}"
CAPTURE_PLAN_MARKDOWN="${CITADEL_CAPTURE_DIAGNOSTICS_PLAN_MARKDOWN:-$ROOT/reports/citadel_replay_capture_plan.md}"
ADVANCE_WORKFLOW_EVIDENCE="${CITADEL_CAPTURE_ADVANCE_WORKFLOW_EVIDENCE:-0}"
APPLY_WORKFLOW_EVIDENCE="${CITADEL_CAPTURE_APPLY_WORKFLOW_EVIDENCE:-0}"
ADVANCE_REPORT="${CITADEL_CAPTURE_WORKFLOW_EVIDENCE_ADVANCE_REPORT:-$ROOT/reports/mortgage_workflow_evidence_advance_90-madison-ave.json}"
INSTALL_VERIFIED_HAR="${CITADEL_CAPTURE_INSTALL_VERIFIED_HAR:-0}"
INSTALL_VERIFIED_HAR_APPLY="${CITADEL_CAPTURE_INSTALL_VERIFIED_HAR_APPLY:-$APPLY_WORKFLOW_EVIDENCE}"
INSTALL_VERIFIED_HAR_ONLY="${CITADEL_CAPTURE_INSTALL_VERIFIED_HAR_ONLY:-0}"
INSTALL_VERIFIED_HAR_REPORT="${CITADEL_CAPTURE_INSTALL_VERIFIED_HAR_REPORT:-$ROOT/reports/citadel_verified_capture_install_report.json}"
CANONICAL_HAR_PATH="${CITADEL_CANONICAL_HAR_PATH:-/mnt/f/har/citadel_loansphereservicingdigital.bkiconnect.com.har}"
SKIP_CAPTURE="${CITADEL_CAPTURE_SKIP_CAPTURE:-0}"
VALIDATE_ONLY="${CITADEL_CAPTURE_VALIDATE_ONLY:-0}"
PY="${PYTHON_BIN:-python3}"

if [ "$SKIP_CAPTURE" = "1" ]; then
  echo "[citadel] CITADEL_CAPTURE_SKIP_CAPTURE=1; validating existing capture artifacts without opening a browser."
  CAPTURE_RC=0
else
  set +e
  WORKSPACE_ROOT="$ROOT" \
  CITADEL_CAPTURE_HAR_PATH="$CAPTURE_HAR_PATH" \
  CITADEL_CAPTURE_REPORT="$CAPTURE_REPORT" \
  "$ROOT/scripts/citadel_manual_capture_har.sh"
  CAPTURE_RC=$?
  set -e
fi

if [ "$CAPTURE_RC" -ne 0 ]; then
  echo "[citadel] Capture step exited rc=$CAPTURE_RC; running replay diagnostics before deciding." >&2
fi

echo "[citadel] Workflow capture written for diagnostics only: $CAPTURE_HAR_PATH"
set +e
WORKSPACE_ROOT="$ROOT" \
CITADEL_HAR_PATH="$CAPTURE_HAR_PATH" \
CITADEL_HAR_DIAGNOSTICS_REPORT="$CAPTURE_DIAGNOSTICS_REPORT" \
CITADEL_CAPTURE_PLAN_REPORT="$CAPTURE_PLAN_REPORT" \
CITADEL_CAPTURE_PLAN_MARKDOWN="$CAPTURE_PLAN_MARKDOWN" \
node "$ROOT/scripts/citadel_har_workflow_diagnostics.js"
DIAGNOSTICS_RC=$?
set -e

set +e
"$PY" - "$CAPTURE_REPORT" "$CAPTURE_DIAGNOSTICS_REPORT" "$CAPTURE_RC" "$DIAGNOSTICS_RC" <<'PY' >&2
import json
import sys
from pathlib import Path

capture_report_path = Path(sys.argv[1])
diagnostics_report_path = Path(sys.argv[2])
capture_rc = int(sys.argv[3])
diagnostics_rc = int(sys.argv[4])


def load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:
        return {"status": "error", "reason": f"report_parse_error:{exc}"}


def countish(value):
    try:
        return int(value or 0)
    except Exception:
        return 0


def nonempty(value):
    return bool(value) if isinstance(value, (list, dict, str)) else False


def compact_json(value):
    if value in (None, [], {}):
        return value
    return json.dumps(value, sort_keys=True)


capture = load_json(capture_report_path)
diagnostics = load_json(diagnostics_report_path)
next_action = diagnostics.get("next_action") if isinstance(diagnostics, dict) and isinstance(diagnostics.get("next_action"), dict) else {}
capture_next_action = capture.get("next_action") if isinstance(capture, dict) and isinstance(capture.get("next_action"), dict) else {}
blockers = []

if capture_rc != 0:
    blockers.append(f"capture_rc={capture_rc}")
if diagnostics_rc != 0:
    blockers.append(f"diagnostics_rc={diagnostics_rc}")
if not isinstance(diagnostics, dict):
    blockers.append("diagnostics_report_missing")
elif diagnostics.get("status") != "ok":
    blockers.append(f"diagnostics_status={diagnostics.get('status')}")
elif diagnostics.get("can_replay_documents") is not True:
    blockers.append("diagnostics_can_replay_documents=false")
if isinstance(diagnostics, dict) and countish(diagnostics.get("missing_response_body_count")) > 0:
    blockers.append(f"missing_response_body_count={diagnostics.get('missing_response_body_count')}")
if next_action and next_action.get("capture_required") is not False:
    blockers.append(f"next_action_status={next_action.get('status')}")

if not isinstance(capture, dict):
    blockers.append("capture_report_missing")
else:
    if capture.get("status") != "ok":
        blockers.append(f"capture_status={capture.get('status')}")
    if capture.get("reason"):
        blockers.append(f"capture_reason={capture.get('reason')}")
    if countish(capture.get("replayable_document_payload_count")) <= 0:
        blockers.append("capture_replayable_document_payload_count=0")
    if "target_month_replayable_document_available" in capture and capture.get("target_month_replayable_document_available") is not True:
        blockers.append(f"target_month_replayable_document_available={str(capture.get('target_month_replayable_document_available')).lower()}")
    if "target_month_replayable_document_payload_count" in capture and countish(capture.get("target_month_replayable_document_payload_count")) <= 0:
        blockers.append("target_month_replayable_document_payload_count=0")
    if nonempty(capture.get("missing_required_response_paths")):
        blockers.append("missing_required_response_paths")
    if nonempty(capture.get("missing_required_response_path_counts")):
        blockers.append("missing_required_response_path_counts")
    if countish(capture.get("missing_response_body_requirement_count")) > 0:
        blockers.append(f"missing_response_body_requirement_count={capture.get('missing_response_body_requirement_count')}")
    if nonempty(capture.get("missing_response_body_requirements")):
        blockers.append("missing_response_body_requirements")
    if capture_next_action and capture_next_action.get("capture_required") is not False:
        blockers.append(f"capture_next_action_status={capture_next_action.get('status')}")

if blockers:
    print("[citadel] Captured workflow HAR failed replay gate: " + ", ".join(blockers))
    if isinstance(capture, dict):
        print(f"[citadel] capture_report={capture_report_path}")
        print(f"[citadel] capture_status={capture.get('status')}")
        print(f"[citadel] capture_reason={capture.get('reason')}")
        print(f"[citadel] capture_target_month={capture.get('target_month')}")
        print(f"[citadel] capture_target_month_replayable_document_available={capture.get('target_month_replayable_document_available')}")
        print(f"[citadel] capture_target_month_replayable_document_payload_count={capture.get('target_month_replayable_document_payload_count')}")
        print(f"[citadel] capture_replayable_statement_months={compact_json(capture.get('replayable_statement_months'))}")
        print(f"[citadel] capture_statement_document_months={compact_json(capture.get('statement_document_months'))}")
        print(f"[citadel] capture_required_response_paths={compact_json(capture.get('required_response_paths'))}")
        print(f"[citadel] capture_required_response_path_counts={compact_json(capture.get('required_response_path_counts'))}")
        print(f"[citadel] capture_missing_required_response_paths={compact_json(capture.get('missing_required_response_paths'))}")
        print(f"[citadel] capture_missing_required_response_path_counts={compact_json(capture.get('missing_required_response_path_counts'))}")
        print(f"[citadel] capture_response_body_requirements={compact_json(capture.get('response_body_requirements'))}")
        print(f"[citadel] capture_missing_response_body_requirements={compact_json(capture.get('missing_response_body_requirements'))}")
        print(f"[citadel] capture_next_action_status={capture_next_action.get('status')}")
        print(f"[citadel] capture_next_action_command={capture_next_action.get('next_command')}")
    if isinstance(diagnostics, dict):
        print(f"[citadel] diagnostics_report={diagnostics_report_path}")
        print(f"[citadel] capture_quality_status={diagnostics.get('capture_quality_status')}")
        print(f"[citadel] replay_blocker={diagnostics.get('replay_blocker')}")
        print(f"[citadel] missing_response_body_paths={compact_json(diagnostics.get('missing_response_body_paths'))}")
        print(f"[citadel] missing_response_body_path_counts={compact_json(diagnostics.get('missing_response_body_path_counts'))}")
        print(f"[citadel] response_body_requirements={compact_json(diagnostics.get('response_body_requirements'))}")
        print(f"[citadel] next_action_status={next_action.get('status')}")
        print(f"[citadel] next_action_command={next_action.get('next_command')}")
    raise SystemExit(1)

print(f"[citadel] capture_report={capture_report_path}")
print(f"[citadel] diagnostics_report={diagnostics_report_path}")
print("[citadel] Captured workflow HAR passed replay gate.")
PY
GATE_RC=$?
set -e

if [ "$GATE_RC" -ne 0 ]; then
  echo "[citadel] Captured workflow HAR is not replayable yet; not attempting statement download." >&2
  echo "[citadel] Diagnostics report: $CAPTURE_DIAGNOSTICS_REPORT" >&2
  "$PY" - "$CAPTURE_DIAGNOSTICS_REPORT" <<'PY' >&2 || true
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
if not p.exists():
    raise SystemExit(0)
data = json.loads(p.read_text(encoding="utf-8"))
next_action = data.get("next_action") or {}
print(f"[citadel] capture_quality_status={data.get('capture_quality_status')}")
print(f"[citadel] replay_blocker={data.get('replay_blocker')}")
print(f"[citadel] missing_response_body_paths={json.dumps(data.get('missing_response_body_paths') or [], sort_keys=True)}")
print(f"[citadel] missing_response_body_path_counts={json.dumps(data.get('missing_response_body_path_counts') or {}, sort_keys=True)}")
print(f"[citadel] response_body_requirements={json.dumps(data.get('response_body_requirements') or [], sort_keys=True)}")
print(f"[citadel] next_action_status={next_action.get('status')}")
print(f"[citadel] next_action_command={next_action.get('next_command')}")
PY
  if [ "$DIAGNOSTICS_RC" -ne 0 ]; then
    exit "$DIAGNOSTICS_RC"
  fi
  if [ "$CAPTURE_RC" -ne 0 ]; then
    exit "$CAPTURE_RC"
  fi
  exit "$GATE_RC"
fi
echo "[citadel] Captured workflow HAR is replayable; running statement downloaders in explicit HAR replay mode."
if [ "$VALIDATE_ONLY" = "1" ]; then
  echo "[citadel] CITADEL_CAPTURE_VALIDATE_ONLY=1; replay gate passed, stopping before workflow evidence advance and statement download."
  exit 0
fi
if [ "$INSTALL_VERIFIED_HAR" = "1" ]; then
  echo "[citadel] Installing verified captured HAR before downloader replay."
  INSTALL_ARGS=(
    "$ROOT/scripts/install_verified_citadel_capture_har.py"
    --capture-har "$CAPTURE_HAR_PATH"
    --capture-report "$CAPTURE_REPORT"
    --canonical-har "$CANONICAL_HAR_PATH"
    --report "$INSTALL_VERIFIED_HAR_REPORT"
  )
  if [ "$INSTALL_VERIFIED_HAR_APPLY" = "1" ]; then
    INSTALL_ARGS+=(--apply)
  fi
  set +e
  WORKSPACE_ROOT="$ROOT" "$PY" "${INSTALL_ARGS[@]}"
  INSTALL_RC=$?
  set -e
  if [ "$INSTALL_RC" -ne 0 ]; then
    echo "[citadel] Verified captured HAR install failed rc=$INSTALL_RC; not attempting statement download." >&2
    echo "[citadel] Install report: $INSTALL_VERIFIED_HAR_REPORT" >&2
    exit "$INSTALL_RC"
  fi
  if [ "$INSTALL_VERIFIED_HAR_APPLY" = "1" ]; then
    echo "[citadel] Verified captured HAR install applied: $INSTALL_VERIFIED_HAR_REPORT"
  else
    echo "[citadel] Verified captured HAR install dry-run report: $INSTALL_VERIFIED_HAR_REPORT"
  fi
  if [ "$INSTALL_VERIFIED_HAR_ONLY" = "1" ]; then
    echo "[citadel] CITADEL_CAPTURE_INSTALL_VERIFIED_HAR_ONLY=1; install gate passed, stopping before workflow evidence advance and statement download."
    exit 0
  fi
fi
if [ "$ADVANCE_WORKFLOW_EVIDENCE" = "1" ]; then
  echo "[citadel] Advancing captured workflow evidence before downloader replay."
  ADVANCE_ARGS=(
    "$ROOT/scripts/advance_mortgage_workflow_evidence.py"
    --property "90 Madison Ave"
    --har "90 Madison Ave=$CAPTURE_HAR_PATH"
    --intake "$ROOT/config/mortgage_downloader_intake.json"
    --registry "$ROOT/config/mortgage_statement_downloaders.json"
    --workspace-root "$ROOT"
    --report-dir "$ROOT/reports"
    --report "$ADVANCE_REPORT"
    --write-stubs
    --install-registry-entries
  )
  TARGET_MONTH="${CITADEL_TARGET_MONTH:-${MORTGAGE_STATEMENT_TARGET_MONTH:-${BASELANE_MORTGAGE_STATEMENT_TARGET_MONTH:-${BASELANE_MONTHLY_TARGET_STAMP:-}}}}"
  if [ -n "$TARGET_MONTH" ]; then
    ADVANCE_ARGS+=(--target-month "$TARGET_MONTH")
  fi
  if [ "$APPLY_WORKFLOW_EVIDENCE" = "1" ]; then
    ADVANCE_ARGS+=(--apply-evidence --apply-registry)
  fi
  WORKSPACE_ROOT="$ROOT" "$PY" "${ADVANCE_ARGS[@]}"
  echo "[citadel] Captured workflow evidence advance report: $ADVANCE_REPORT"
fi
WORKSPACE_ROOT="$ROOT" \
MORTGAGE_DOWNLOADER_ALLOW_HAR_MODE=1 \
MORTGAGE_DOWNLOADER_PROFILE="${MORTGAGE_DOWNLOADER_PROFILE:-har_replay}" \
CITADEL_LOGIN_MODE=har \
CITADEL_HAR_PATH="$CAPTURE_HAR_PATH" \
"$PY" "$ROOT/scripts/run_mortgage_statement_downloaders.py"
