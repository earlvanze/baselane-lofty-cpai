#!/usr/bin/env bash
set -euo pipefail

# Idempotent monthly mortgage workflow gate.
#
# This script is invoked by baselane_monthly_statements_idempotent.sh after
# statement capture. It performs two jobs, each independently idempotent:
#
#   1. Download latest mortgage statements from co-owner-paid servicers
#      registered in config/mortgage_statement_downloaders.json.
#   2. Run Baselane native mortgage splits for Citadel transactions.
#
# State file: scripts/.baselane_monthly_mortgage_workflow_state
# Stamp format: YYYY-MM (previous month, matching the monthly gate).

OPENCLAW_ROOT="${OPENCLAW_ROOT:-$HOME/.openclaw}"
ROOT="${WORKSPACE_ROOT:-$OPENCLAW_ROOT/workspace}"
PY="${PYTHON_BIN:-python3}"
NODE="${NODE_BIN:-node}"
REPORT_DIR="${BASELANE_REPORT_DIR:-$ROOT/reports}"
STATE_FILE="${BASELANE_MORTGAGE_WORKFLOW_STATE_FILE:-$ROOT/scripts/.baselane_monthly_mortgage_workflow_state}"
MORTGAGE_DOWNLOADER_RUNNER="${MORTGAGE_DOWNLOADER_RUNNER:-$ROOT/scripts/run_mortgage_statement_downloaders.py}"
MORTGAGE_DOWNLOADER_REPORT="${MORTGAGE_DOWNLOADER_REPORT:-$REPORT_DIR/mortgage_statement_downloaders_report.json}"
MORTGAGE_DOWNLOADER_WORKFLOW_TIMEOUT_SECONDS="${BASELANE_MORTGAGE_DOWNLOADER_WORKFLOW_TIMEOUT_SECONDS:-180}"
MORTGAGE_WORKFLOW_STEP_TIMEOUT_SECONDS="${BASELANE_MORTGAGE_WORKFLOW_STEP_TIMEOUT_SECONDS:-180}"
MORTGAGE_WORKFLOW_STEP_KILL_AFTER_SECONDS="${BASELANE_MORTGAGE_WORKFLOW_STEP_KILL_AFTER_SECONDS:-15}"
CITADEL_HAR_REPLAY_RUNNER_REPORT="${CITADEL_HAR_REPLAY_RUNNER_REPORT:-$REPORT_DIR/citadel_har_replay_runner_report.json}"
MORTGAGE_WORKFLOW_REPORT_ONLY_RUN_HAR_REPLAY="${MORTGAGE_WORKFLOW_REPORT_ONLY_RUN_HAR_REPLAY:-0}"
MORTGAGE_WORKFLOW_REPORT_ONLY_HAR_REPLAY_TIMEOUT_SECONDS="${MORTGAGE_WORKFLOW_REPORT_ONLY_HAR_REPLAY_TIMEOUT_SECONDS:-60}"
CITADEL_AUTH_PREFLIGHT_SCRIPT="${CITADEL_AUTH_PREFLIGHT_SCRIPT:-$ROOT/scripts/citadel_auth_preflight.js}"
CITADEL_AUTH_PREFLIGHT_REPORT="${CITADEL_AUTH_PREFLIGHT_REPORT:-$REPORT_DIR/citadel_auth_preflight_report.json}"
CITADEL_HAR_DIAGNOSTICS_SCRIPT="${CITADEL_HAR_DIAGNOSTICS_SCRIPT:-$ROOT/scripts/citadel_har_workflow_diagnostics.js}"
CITADEL_HAR_DIAGNOSTICS_REPORT="${CITADEL_HAR_DIAGNOSTICS_REPORT:-$REPORT_DIR/citadel_har_workflow_diagnostics_report.json}"
CITADEL_CAPTURE_PLAN_REPORT="${CITADEL_CAPTURE_PLAN_REPORT:-$REPORT_DIR/citadel_capture_plan.json}"
CITADEL_CAPTURE_PLAN_MARKDOWN="${CITADEL_CAPTURE_PLAN_MARKDOWN:-$REPORT_DIR/citadel_capture_plan.md}"
CITADEL_CAPTURE_MANUAL_AUTH_FILE="${CITADEL_CAPTURE_MANUAL_AUTH_FILE:-$REPORT_DIR/citadel_manual_capture_required.json}"
MORTGAGE_SPLITS_SCRIPT="${BASELANE_MORTGAGE_SPLITS_SCRIPT:-$ROOT/scripts/baselane_mortgage_splits.py}"
NO_MORTGAGE_FINANCIALS_GUARD_SCRIPT="${BASELANE_NO_MORTGAGE_FINANCIALS_GUARD_SCRIPT:-$ROOT/scripts/baselane_no_mortgage_financials_guard.py}"
TOKENOMICS_SCRIPT="${COOWNERSHIP_MORTGAGE_TOKENOMICS_SCRIPT:-$ROOT/scripts/update_coownership_mortgage_tokenomics.py}"
TOKENOMICS_REPORT="${COOWNERSHIP_MORTGAGE_TOKENOMICS_REPORT:-$REPORT_DIR/coownership_mortgage_tokenomics_update_report.json}"
MORTGAGE_ARCHIVE_RESTORE_SCRIPT="${MORTGAGE_ARCHIVE_RESTORE_SCRIPT:-$ROOT/scripts/restore_coownership_mortgage_archive_candidates.py}"
MORTGAGE_ARCHIVE_RESTORE_REPORT="${MORTGAGE_ARCHIVE_RESTORE_REPORT:-$REPORT_DIR/coownership_mortgage_archive_restore_current_month_report.json}"
MORTGAGE_ARCHIVE_RESTORE_MARKDOWN="${MORTGAGE_ARCHIVE_RESTORE_MARKDOWN:-$REPORT_DIR/coownership_mortgage_archive_restore_current_month_report.md}"
MORTGAGE_COVERAGE_SCRIPT="${MORTGAGE_DOWNLOADER_COVERAGE_SCRIPT:-$ROOT/scripts/audit_mortgage_downloader_coverage.py}"
MORTGAGE_COVERAGE_REPORT="${MORTGAGE_DOWNLOADER_COVERAGE_REPORT:-$REPORT_DIR/mortgage_downloader_coverage_report.json}"
MORTGAGE_CREDENTIAL_REFRESH_REPORT="${MORTGAGE_WORKFLOW_CREDENTIAL_REFRESH_REPORT:-$REPORT_DIR/mortgage_statement_downloaders_credential_refresh_report.json}"
MORTGAGE_EVIDENCE_REFRESH_SCRIPT="${MORTGAGE_WORKFLOW_EVIDENCE_REFRESH_SCRIPT:-$ROOT/scripts/mortgage_workflow_evidence_refresh.py}"
MORTGAGE_EVIDENCE_REFRESH_REPORT="${MORTGAGE_WORKFLOW_EVIDENCE_REFRESH_REPORT:-$REPORT_DIR/mortgage_workflow_evidence_refresh_report.json}"
MORTGAGE_EVIDENCE_ADVANCE_SCRIPT="${MORTGAGE_WORKFLOW_EVIDENCE_ADVANCE_SCRIPT:-$ROOT/scripts/advance_mortgage_workflow_evidence.py}"
MORTGAGE_EVIDENCE_ADVANCE_REPORT="${MORTGAGE_WORKFLOW_EVIDENCE_ADVANCE_REPORT:-$REPORT_DIR/mortgage_workflow_evidence_advance_report.json}"
MORTGAGE_REPLAYABILITY_SCRIPT="${MORTGAGE_WORKFLOW_REPLAYABILITY_SCRIPT:-$ROOT/scripts/mortgage_workflow_replayability_gate.py}"
MORTGAGE_REPLAYABILITY_REPORT="${MORTGAGE_WORKFLOW_REPLAYABILITY_REPORT:-$REPORT_DIR/mortgage_workflow_replayability_gate_report.json}"
MORTGAGE_CAPTURE_QUEUE_SCRIPT="${MORTGAGE_WORKFLOW_CAPTURE_QUEUE_SCRIPT:-$ROOT/scripts/mortgage_workflow_capture_queue.py}"
MORTGAGE_CAPTURE_QUEUE_REPORT="${MORTGAGE_WORKFLOW_CAPTURE_QUEUE_REPORT:-$REPORT_DIR/mortgage_workflow_capture_queue.json}"
MORTGAGE_CAPTURE_QUEUE_MARKDOWN="${MORTGAGE_WORKFLOW_CAPTURE_QUEUE_MARKDOWN:-$REPORT_DIR/mortgage_workflow_capture_queue.md}"
MORTGAGE_CAPTURE_QUEUE_PROCESSOR_SCRIPT="${MORTGAGE_WORKFLOW_CAPTURE_QUEUE_PROCESSOR_SCRIPT:-$ROOT/scripts/process_mortgage_capture_queue.py}"
MORTGAGE_CAPTURE_QUEUE_PROCESSOR_REPORT="${MORTGAGE_WORKFLOW_CAPTURE_QUEUE_PROCESSOR_REPORT:-$REPORT_DIR/mortgage_workflow_capture_queue_processor.json}"
MORTGAGE_CAPTURE_QUEUE_PROCESSOR_MARKDOWN="${MORTGAGE_WORKFLOW_CAPTURE_QUEUE_PROCESSOR_MARKDOWN:-$REPORT_DIR/mortgage_workflow_capture_queue_processor.md}"
GATE_REPORT="${MORTGAGE_WORKFLOW_GATE_REPORT:-$REPORT_DIR/baselane_monthly_mortgage_workflow_gate_report.json}"
REVIEW_PACKET="${MORTGAGE_WORKFLOW_REVIEW_PACKET:-$REPORT_DIR/baselane_monthly_mortgage_workflow_review_packet.md}"
REPAIR_PACKET="${MORTGAGE_WORKFLOW_REPAIR_PACKET:-$REPORT_DIR/mortgage_workflow_repair_packet.json}"
REPAIR_PACKET_MARKDOWN="${MORTGAGE_WORKFLOW_REPAIR_PACKET_MARKDOWN:-$REPORT_DIR/mortgage_workflow_repair_packet.md}"

YEAR="${BASELANE_MONTHLY_GATE_YEAR:-$(date +%Y)}"
MONTH="${BASELANE_MONTHLY_GATE_MONTH:-$(date +%m)}"
DRY_RUN="${DRY_RUN:-0}"
if [ "$MONTH" = "01" ]; then
  PREV_MONTH=12
  PREV_YEAR=$((YEAR - 1))
else
  PREV_MONTH=$((10#$MONTH - 1))
  PREV_YEAR=$YEAR
fi
STAMP="${BASELANE_MONTHLY_TARGET_STAMP:-${PREV_YEAR}-$(printf '%02d' "$PREV_MONTH")}"
MORTGAGE_STATEMENT_TARGET_MONTH="${MORTGAGE_STATEMENT_TARGET_MONTH:-${BASELANE_MORTGAGE_STATEMENT_TARGET_MONTH:-${YEAR}-$(printf '%02d' "$MONTH")}}"
CITADEL_STATEMENT_TARGET_MONTH="${CITADEL_STATEMENT_TARGET_MONTH:-$MORTGAGE_STATEMENT_TARGET_MONTH}"
DOWNLOADER_CITADEL_TARGET_MONTH="${MORTGAGE_DOWNLOADER_CITADEL_TARGET_MONTH:-$CITADEL_STATEMENT_TARGET_MONTH}"
MORTGAGE_DOWNLOADER_REPORT_ONLY_SUMMARIZED="${MORTGAGE_DOWNLOADER_REPORT_ONLY_SUMMARIZED:-0}"
MORTGAGE_SPLITS_REPORT="${BASELANE_MORTGAGE_SPLITS_REPORT:-$REPORT_DIR/baselane_mortgage_splits_${STAMP}.json}"
NO_MORTGAGE_FINANCIALS_GUARD_REPORT="${BASELANE_NO_MORTGAGE_FINANCIALS_GUARD_REPORT:-$REPORT_DIR/baselane_no_mortgage_financials_guard_${STAMP}.json}"
MORTGAGE_WORKFLOW_STEP_TIMEOUT_ARGS=()
if command -v timeout >/dev/null 2>&1 && [ "$MORTGAGE_WORKFLOW_STEP_TIMEOUT_SECONDS" != "0" ]; then
  MORTGAGE_WORKFLOW_STEP_TIMEOUT_ARGS=(
    timeout
    --kill-after="${MORTGAGE_WORKFLOW_STEP_KILL_AFTER_SECONDS}s"
    "${MORTGAGE_WORKFLOW_STEP_TIMEOUT_SECONDS}s"
  )
fi

mkdir -p "$REPORT_DIR" "$(dirname "$STATE_FILE")"

write_gate_report() {
  local status="$1"
  local reason="${2:-}"
  local downloader_rc="${3:-0}"
  local citadel_rc="${4:-0}"
  local splits_rc="${5:-0}"
  local tokenomics_rc="${6:-0}"
  local coverage_rc="${7:-0}"
  BASELANE_MORTGAGE_GATE_STATUS="$status" \
  BASELANE_MORTGAGE_GATE_REASON="$reason" \
  BASELANE_MORTGAGE_GATE_STAMP="$STAMP" \
  BASELANE_MORTGAGE_STATEMENT_TARGET_MONTH="$MORTGAGE_STATEMENT_TARGET_MONTH" \
  BASELANE_MORTGAGE_CITADEL_STATEMENT_TARGET_MONTH="$DOWNLOADER_CITADEL_TARGET_MONTH" \
  BASELANE_MORTGAGE_GATE_STATE_FILE="$STATE_FILE" \
  BASELANE_MORTGAGE_GATE_DOWNLOADER_RC="$downloader_rc" \
  BASELANE_MORTGAGE_GATE_CITADEL_RC="$citadel_rc" \
  BASELANE_MORTGAGE_GATE_SPLITS_RC="$splits_rc" \
  BASELANE_MORTGAGE_GATE_TOKENOMICS_RC="$tokenomics_rc" \
  BASELANE_MORTGAGE_GATE_COVERAGE_RC="$coverage_rc" \
  BASELANE_MORTGAGE_GATE_REPORT_ONLY="${MORTGAGE_WORKFLOW_REPORT_ONLY:-0}" \
  BASELANE_MORTGAGE_DOWNLOADER_EXACT_TARGET_MONTH_MODE="${MORTGAGE_WORKFLOW_REPORT_ONLY_DOWNLOADER_EXACT_TARGET_MONTH:-1}" \
  BASELANE_MORTGAGE_SPLITS_REPORT="$MORTGAGE_SPLITS_REPORT" \
  BASELANE_NO_MORTGAGE_FINANCIALS_GUARD_REPORT="$NO_MORTGAGE_FINANCIALS_GUARD_REPORT" \
  BASELANE_NO_MORTGAGE_FINANCIALS_GUARD_RC="${NO_MORTGAGE_FINANCIALS_GUARD_RC:-0}" \
  BASELANE_MORTGAGE_ARCHIVE_RESTORE_REPORT="$MORTGAGE_ARCHIVE_RESTORE_REPORT" \
  BASELANE_MORTGAGE_ARCHIVE_RESTORE_MARKDOWN="$MORTGAGE_ARCHIVE_RESTORE_MARKDOWN" \
  BASELANE_MORTGAGE_ARCHIVE_RESTORE_RC="${MORTGAGE_ARCHIVE_RESTORE_RC:-0}" \
  BASELANE_MORTGAGE_DOWNLOADER_REPORT_ONLY_SUMMARIZED="$MORTGAGE_DOWNLOADER_REPORT_ONLY_SUMMARIZED" \
  BASELANE_MORTGAGE_DOWNLOADER_REPORT="$MORTGAGE_DOWNLOADER_REPORT" \
  BASELANE_MORTGAGE_DOWNLOADER_WORKFLOW_TIMEOUT_SECONDS="$MORTGAGE_DOWNLOADER_WORKFLOW_TIMEOUT_SECONDS" \
  BASELANE_MORTGAGE_WORKFLOW_STEP_TIMEOUT_SECONDS="$MORTGAGE_WORKFLOW_STEP_TIMEOUT_SECONDS" \
  BASELANE_MORTGAGE_WORKFLOW_STEP_KILL_AFTER_SECONDS="$MORTGAGE_WORKFLOW_STEP_KILL_AFTER_SECONDS" \
  BASELANE_MORTGAGE_CREDENTIAL_REFRESH_REPORT="$MORTGAGE_CREDENTIAL_REFRESH_REPORT" \
  BASELANE_MORTGAGE_CREDENTIAL_REFRESH_RC="${MORTGAGE_CREDENTIAL_REFRESH_RC:-0}" \
  BASELANE_MORTGAGE_CREDENTIAL_REFRESH_STATUS="${MORTGAGE_CREDENTIAL_REFRESH_STATUS:-skipped}" \
  BASELANE_MORTGAGE_CREDENTIAL_REFRESH_PROFILE="${MORTGAGE_CREDENTIAL_REFRESH_PROFILE:-}" \
  BASELANE_MORTGAGE_CREDENTIAL_REFRESH_IDS="${MORTGAGE_CREDENTIAL_REFRESH_IDS:-}" \
  BASELANE_MORTGAGE_CREDENTIAL_REFRESH_TIMEOUT_SECONDS="${MORTGAGE_CREDENTIAL_REFRESH_TIMEOUT_SECONDS:-}" \
  BASELANE_MORTGAGE_CITADEL_HAR_DIAGNOSTICS_REPORT="$CITADEL_HAR_DIAGNOSTICS_REPORT" \
  BASELANE_MORTGAGE_CITADEL_CAPTURE_PLAN_REPORT="$CITADEL_CAPTURE_PLAN_REPORT" \
  BASELANE_MORTGAGE_CITADEL_CAPTURE_PLAN_MARKDOWN="$CITADEL_CAPTURE_PLAN_MARKDOWN" \
  BASELANE_MORTGAGE_CITADEL_CAPTURE_MANUAL_AUTH_FILE="$CITADEL_CAPTURE_MANUAL_AUTH_FILE" \
  BASELANE_MORTGAGE_CITADEL_HAR_DIAGNOSTICS_RC="${CITADEL_HAR_DIAGNOSTICS_RC:-0}" \
  BASELANE_MORTGAGE_CITADEL_AUTH_PREFLIGHT_REPORT="$CITADEL_AUTH_PREFLIGHT_REPORT" \
  BASELANE_MORTGAGE_CITADEL_AUTH_PREFLIGHT_RC="${CITADEL_PREFLIGHT_RC:-0}" \
  BASELANE_MORTGAGE_TOKENOMICS_REPORT="$TOKENOMICS_REPORT" \
  BASELANE_MORTGAGE_COVERAGE_REPORT="$MORTGAGE_COVERAGE_REPORT" \
  BASELANE_MORTGAGE_EVIDENCE_ADVANCE_REPORT="$MORTGAGE_EVIDENCE_ADVANCE_REPORT" \
  BASELANE_MORTGAGE_EVIDENCE_ADVANCE_RC="${MORTGAGE_EVIDENCE_ADVANCE_RC:-0}" \
  BASELANE_MORTGAGE_EVIDENCE_REFRESH_REPORT="$MORTGAGE_EVIDENCE_REFRESH_REPORT" \
  BASELANE_MORTGAGE_EVIDENCE_REFRESH_RC="${MORTGAGE_EVIDENCE_REFRESH_RC:-0}" \
  BASELANE_MORTGAGE_REPLAYABILITY_REPORT="$MORTGAGE_REPLAYABILITY_REPORT" \
  BASELANE_MORTGAGE_REPLAYABILITY_RC="${MORTGAGE_REPLAYABILITY_RC:-0}" \
  BASELANE_MORTGAGE_CAPTURE_QUEUE_REPORT="$MORTGAGE_CAPTURE_QUEUE_REPORT" \
  BASELANE_MORTGAGE_CAPTURE_QUEUE_MARKDOWN="$MORTGAGE_CAPTURE_QUEUE_MARKDOWN" \
  BASELANE_MORTGAGE_CAPTURE_QUEUE_RC="${MORTGAGE_CAPTURE_QUEUE_RC:-0}" \
  BASELANE_MORTGAGE_CAPTURE_QUEUE_PROCESSOR_REPORT="$MORTGAGE_CAPTURE_QUEUE_PROCESSOR_REPORT" \
  BASELANE_MORTGAGE_CAPTURE_QUEUE_PROCESSOR_MARKDOWN="$MORTGAGE_CAPTURE_QUEUE_PROCESSOR_MARKDOWN" \
  BASELANE_MORTGAGE_CAPTURE_QUEUE_PROCESSOR_RC="${MORTGAGE_CAPTURE_QUEUE_PROCESSOR_RC:-0}" \
  BASELANE_MORTGAGE_TOKENOMICS_WRITE_GUARD_STATUS="${TOKENOMICS_WRITE_GUARD_STATUS:-not_evaluated}" \
  BASELANE_MORTGAGE_TOKENOMICS_WRITE_GUARD_REASON="${TOKENOMICS_WRITE_GUARD_REASON:-}" \
  BASELANE_MORTGAGE_DOWNLOADER_TOKENOMICS_BLOCKING_GAP_COUNT="${TOKENOMICS_WRITE_GUARD_DOWNLOADER_BLOCKING_GAP_COUNT:-0}" \
  BASELANE_MORTGAGE_DOWNLOADER_TOKENOMICS_BLOCKING_GAP_PROPERTIES="${TOKENOMICS_WRITE_GUARD_DOWNLOADER_BLOCKING_GAP_PROPERTIES:-[]}" \
  BASELANE_MORTGAGE_CITADEL_TOKENOMICS_BLOCKING="${TOKENOMICS_WRITE_GUARD_CITADEL_BLOCKING:-false}" \
  BASELANE_MORTGAGE_CAPTURE_QUEUE_TOKENOMICS_BLOCKING_COUNT="${TOKENOMICS_WRITE_GUARD_CAPTURE_QUEUE_BLOCKING_COUNT:-0}" \
  BASELANE_MORTGAGE_CAPTURE_QUEUE_TOKENOMICS_BLOCKING_PROPERTIES="${TOKENOMICS_WRITE_GUARD_CAPTURE_QUEUE_BLOCKING_PROPERTIES:-[]}" \
  BASELANE_MORTGAGE_CAPTURE_QUEUE_TOKENOMICS_NON_BLOCKING_COUNT="${TOKENOMICS_WRITE_GUARD_CAPTURE_QUEUE_NON_BLOCKING_COUNT:-0}" \
  BASELANE_MORTGAGE_CAPTURE_QUEUE_TOKENOMICS_NON_BLOCKING_PROPERTIES="${TOKENOMICS_WRITE_GUARD_CAPTURE_QUEUE_NON_BLOCKING_PROPERTIES:-[]}" \
  BASELANE_MORTGAGE_TOKENOMICS_TRUTH_READY="${TOKENOMICS_WRITE_GUARD_TRUTH_READY:-false}" \
  BASELANE_MORTGAGE_TOKENOMICS_INFRA_REVIEW_IGNORED="${TOKENOMICS_WRITE_GUARD_INFRA_REVIEW_IGNORED:-false}" \
  BASELANE_MORTGAGE_REPORT_ONLY_TOKENOMICS_WRITE_LIVE="${REPORT_ONLY_TOKENOMICS_WRITE_LIVE:-0}" \
  BASELANE_MORTGAGE_GATE_REPORT="$GATE_REPORT" \
  BASELANE_MORTGAGE_REVIEW_PACKET="$REVIEW_PACKET" \
  BASELANE_MORTGAGE_REPAIR_PACKET="$REPAIR_PACKET" \
  BASELANE_MORTGAGE_REPAIR_PACKET_MARKDOWN="$REPAIR_PACKET_MARKDOWN" \
  "$PY" - <<'PY' || true
import hashlib
import json, os
from pathlib import Path


VOLATILE_GATE_REPORT_FIELDS = {"idempotency_digest"}


def write_text_if_changed(path, text):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if target.exists() and target.read_text(encoding="utf-8") == text:
            return
    except Exception:
        pass
    target.write_text(text, encoding="utf-8")


def write_json_if_changed(path, data):
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    write_text_if_changed(path, text)


def env_json(name, fallback):
    try:
        return json.loads(os.environ.get(name) or "")
    except Exception:
        return fallback


def read_json_file(path):
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def fill_missing(mapping, key, value):
    if mapping.get(key) in (None, "", [], {}) and value not in (None, "", [], {}):
        mapping[key] = value


def without_volatile_report_fields(value):
    if isinstance(value, dict):
        return {
            key: without_volatile_report_fields(child)
            for key, child in value.items()
            if key not in VOLATILE_GATE_REPORT_FIELDS
        }
    if isinstance(value, list):
        return [without_volatile_report_fields(child) for child in value]
    return value


def stable_report_digest(value):
    payload = json.dumps(
        without_volatile_report_fields(value),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def queue_action_value(item, *keys):
    if not isinstance(item, dict):
        return None
    for key in keys:
        value = item.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def queue_action_summary(entries):
    if not isinstance(entries, list):
        return []
    actions = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        candidates = item.get("target_month_statement_candidates")
        candidate_pdf_statuses = []
        candidate_document_ids = []
        if isinstance(candidates, list):
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                status = candidate.get("pdf_payload_status")
                if status not in (None, "", [], {}) and status not in candidate_pdf_statuses:
                    candidate_pdf_statuses.append(status)
                document_id = candidate.get("document_identifier") or candidate.get("documentId")
                if document_id not in (None, "", [], {}) and document_id not in candidate_document_ids:
                    candidate_document_ids.append(document_id)
        actions.append({
            "type": queue_action_value(item, "type", "suggested_next_action"),
            "property": item.get("property"),
            "reason": item.get("reason"),
            "har_path": queue_action_value(item, "har_path", "suggested_workflow_har_path"),
            "capture_command": item.get("capture_command"),
            "capture_auth_blocker": item.get("capture_auth_blocker"),
            "capture_auth_blocker_reason": item.get("capture_auth_blocker_reason"),
            "capture_auth_next_action": item.get("capture_auth_next_action"),
            "capture_command_ready_to_run_now": item.get("capture_command_ready_to_run_now"),
            "capture_command_blocked_reason": item.get("capture_command_blocked_reason"),
            "auth_issue": item.get("auth_issue"),
            "manual_auth_reason": item.get("manual_auth_reason"),
            "auth_failure_reason": item.get("auth_failure_reason"),
            "auth_failure_visible_reason": item.get("auth_failure_visible_reason"),
            "command_precondition_status": item.get("command_precondition_status"),
            "command_precondition_reason": item.get("command_precondition_reason"),
            "command_precondition_next_action": item.get("command_precondition_next_action"),
            "existing_workflow_har_path": item.get("existing_workflow_har_path"),
            "existing_workflow_har_path_exists": item.get("existing_workflow_har_path_exists"),
            "existing_workflow_har_path_size": item.get("existing_workflow_har_path_size"),
            "credentials_available": item.get("credentials_available"),
            "credential_source": item.get("credential_source"),
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
            "credential_login_hint_mismatch_overridden": item.get("credential_login_hint_mismatch_overridden"),
            "live_cdp_downloader_command": item.get("live_cdp_downloader_command"),
            "live_cdp_downloader_ready_after_visible_auth": item.get("live_cdp_downloader_ready_after_visible_auth"),
            "source_downloader_id": item.get("source_downloader_id"),
            "source_downloader_manual_auth_required": item.get("source_downloader_manual_auth_required"),
            "source_downloader_operator_next_action": item.get("source_downloader_operator_next_action"),
            "source_downloader_suggested_next_action": item.get("source_downloader_suggested_next_action"),
            "target_month_recapture_reason": item.get("target_month_recapture_reason"),
            "target_month_recapture_required": item.get("target_month_recapture_required"),
            "target_month_statement_candidates": item.get("target_month_statement_candidates"),
            "target_candidate_pdf_statuses": candidate_pdf_statuses,
            "target_candidate_document_ids": candidate_document_ids,
            "duplicate_target_statement_months": item.get("duplicate_target_statement_months"),
            "expected_document_ids": item.get("expected_document_ids"),
            "body_recapture_capture_method": item.get("body_recapture_capture_method"),
            "body_recapture_capture_instruction": item.get("body_recapture_capture_instruction"),
            "body_recapture_manual_har_export_warning": item.get("body_recapture_manual_har_export_warning"),
            "validation_command": queue_action_value(item, "validation_command", "post_capture_check_command"),
            "advance_command": queue_action_value(item, "advance_command", "advance_workflow_evidence_command"),
            "advance_apply_command": queue_action_value(item, "advance_apply_command", "advance_workflow_evidence_apply_command"),
            "tokenomics_blocking": item.get("tokenomics_blocking"),
            "tokenomics_statement_gap_blocking": item.get("tokenomics_statement_gap_blocking"),
            "workbook_write_blocking": item.get("workbook_write_blocking"),
            "tokenomics_capture_priority": item.get("tokenomics_capture_priority"),
            "tokenomics_workbook_write_blocking": item.get("tokenomics_workbook_write_blocking"),
            "tokenomics_write_blocker_count": item.get("tokenomics_write_blocker_count"),
            "tokenomics_write_blocker_reasons": item.get("tokenomics_write_blocker_reasons"),
            "tokenomics_write_blocker_statement_month_statuses": item.get("tokenomics_write_blocker_statement_month_statuses"),
        })
    return actions


def queue_action_values(actions, key):
    values = []
    seen = set()
    for item in actions:
        if not isinstance(item, dict):
            continue
        value = item.get(key)
        if value in (None, "", [], {}):
            continue
        marker = json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
        if marker in seen:
            continue
        seen.add(marker)
        values.append(value)
    return values


def queue_action_properties(actions, key, expected=None):
    properties = []
    seen = set()
    for item in actions:
        if not isinstance(item, dict):
            continue
        value = item.get(key)
        if expected is not None and value != expected:
            continue
        if expected is None and value in (None, "", [], {}):
            continue
        prop = item.get("property")
        if prop in (None, "", [], {}):
            continue
        marker = str(prop)
        if marker in seen:
            continue
        seen.add(marker)
        properties.append(prop)
    return properties


def queue_action_candidate_values(actions, key):
    values = []
    seen = set()
    for item in actions:
        if not isinstance(item, dict):
            continue
        candidates = item.get("target_month_statement_candidates")
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            value = candidate.get(key)
            if value in (None, "", [], {}):
                continue
            marker = json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
            if marker in seen:
                continue
            seen.add(marker)
            values.append(value)
    return values


def summarize_queue_command_preconditions(report, prefix, actions):
    if not isinstance(actions, list):
        actions = []
    report[f"{prefix}_command_precondition_statuses"] = queue_action_values(actions, "command_precondition_status")
    report[f"{prefix}_command_precondition_reasons"] = queue_action_values(actions, "command_precondition_reason")
    report[f"{prefix}_command_precondition_next_actions"] = queue_action_values(actions, "command_precondition_next_action")
    report[f"{prefix}_command_precondition_properties"] = queue_action_properties(actions, "command_precondition_status")
    report[f"{prefix}_blocked_by_auth_properties"] = queue_action_properties(
        actions,
        "command_precondition_status",
        "blocked_by_auth",
    )
    report[f"{prefix}_missing_workflow_har_properties"] = queue_action_properties(
        actions,
        "command_precondition_status",
        "missing_workflow_har",
    )
    report[f"{prefix}_target_month_workflow_har_capture_required_properties"] = queue_action_properties(
        actions,
        "command_precondition_status",
        "target_month_workflow_har_capture_required",
    )
    report[f"{prefix}_existing_workflow_har_paths"] = queue_action_values(actions, "existing_workflow_har_path")
    report[f"{prefix}_existing_workflow_har_path_sizes"] = queue_action_values(actions, "existing_workflow_har_path_size")
    duplicate_months = []
    seen = set()
    for item in actions:
        if not isinstance(item, dict):
            continue
        months = item.get("duplicate_target_statement_months")
        if not isinstance(months, list) or not months:
            continue
        row = {"property": item.get("property"), "duplicate_target_statement_months": months}
        marker = json.dumps(row, sort_keys=True)
        if marker in seen:
            continue
        seen.add(marker)
        duplicate_months.append(row)
    report[f"{prefix}_duplicate_target_statement_months"] = duplicate_months
    report[f"{prefix}_target_month_recapture_reasons"] = queue_action_values(
        actions,
        "target_month_recapture_reason",
    )
    report[f"{prefix}_target_month_recapture_properties"] = queue_action_properties(
        actions,
        "target_month_recapture_reason",
    )
    report[f"{prefix}_target_month_candidate_pdf_payload_statuses"] = queue_action_candidate_values(
        actions,
        "pdf_payload_status",
    )
    report[f"{prefix}_target_month_candidate_document_ids"] = queue_action_candidate_values(
        actions,
        "document_identifier",
    )
    report[f"{prefix}_credential_lookup_statuses"] = queue_action_values(
        actions,
        "credential_lookup_status",
    )
    report[f"{prefix}_credential_lookup_failure_reasons"] = queue_action_values(
        actions,
        "credential_lookup_failure_reason",
    )
    report[f"{prefix}_credential_lookup_absent_values"] = queue_action_values(
        actions,
        "credential_lookup_absent",
    )
    report[f"{prefix}_credential_lookup_misfiled_candidate_counts"] = queue_action_values(
        actions,
        "credential_lookup_misfiled_candidate_count",
    )
    report[f"{prefix}_credential_lookup_repair_actions"] = queue_action_values(
        actions,
        "credential_lookup_repair_action",
    )
    report[f"{prefix}_credential_lookup_item_names"] = queue_action_values(
        actions,
        "credential_lookup_item_name",
    )
    report[f"{prefix}_credential_lookup_expected_folder_names"] = queue_action_values(
        actions,
        "credential_lookup_expected_folder_name",
    )
    report[f"{prefix}_credential_lookup_uri_hosts"] = queue_action_values(
        actions,
        "credential_lookup_uri_host",
    )
    report[f"{prefix}_credential_login_hint_mismatch_values"] = queue_action_values(
        actions,
        "credential_login_hint_mismatch",
    )
    report[f"{prefix}_credential_login_hint_mismatch_properties"] = queue_action_properties(
        actions,
        "credential_login_hint_mismatch",
        True,
    )
    report[f"{prefix}_credential_login_hint_mismatch_overridden_values"] = queue_action_values(
        actions,
        "credential_login_hint_mismatch_overridden",
    )
    report[f"{prefix}_credential_login_hint_mismatch_overridden_properties"] = queue_action_properties(
        actions,
        "credential_login_hint_mismatch_overridden",
        True,
    )


def derive_credential_repair_action(item):
    if not isinstance(item, dict):
        return None
    if item.get("credential_lookup_repair_action"):
        return item.get("credential_lookup_repair_action")
    if (
        item.get("credential_login_hint_mismatch") is True
        and item.get("credential_login_hint_mismatch_overridden") is not True
    ):
        return "update_bitwarden_login_hint_or_force_auto_login"
    if item.get("credential_lookup_status") == "failed":
        failure_reason = item.get("credential_lookup_failure_reason")
        if failure_reason == "item_not_found":
            if item.get("credential_lookup_absent") is True:
                return "create_bitwarden_item"
            misfiled_count = item.get("credential_lookup_misfiled_candidate_count")
            if isinstance(misfiled_count, int) and misfiled_count > 0:
                return "move_existing_bitwarden_item_or_update_expected_folder"
            return "create_or_move_bitwarden_item"
        return "repair_bitwarden_credential_lookup"
    return None


def derive_portal_auth_repair_action(item):
    if not isinstance(item, dict):
        return None
    if item.get("command_precondition_status") != "blocked_by_auth":
        return None
    if (
        item.get("capture_auth_blocker") is False
        and item.get("capture_command_ready_to_run_now") is True
        and item.get("source_downloader_report_status") == "ok"
    ):
        return None
    if item.get("credential_lookup_status") == "failed" or item.get("credentials_available") is False:
        return None
    if (
        item.get("credential_login_hint_mismatch") is True
        and item.get("credential_login_hint_mismatch_overridden") is not True
    ):
        return None
    reason_fields = (
        "capture_command_blocked_reason",
        "capture_auth_blocker_reason",
        "command_precondition_reason",
        "auth_failure_reason",
        "auth_issue",
        "manual_auth_reason",
        "source_downloader_auth_issue",
        "source_downloader_manual_auth_reason",
        "reason",
    )
    reasons = [
        str(item.get(field) or "").strip()
        for field in reason_fields
        if str(item.get(field) or "").strip()
    ]
    if "credentials_unavailable" in reasons:
        return None
    credential_rejected_reasons = {
        "credential_rejected_before_mfa",
        "login_rejected",
        "login_still_required_after_submit",
    }
    if any(reason in credential_rejected_reasons for reason in reasons):
        return "repair_portal_credentials_or_visible_auth"
    if "authenticated_browser_required" in reasons:
        return "authenticate_visible_portal_then_rerun_live_cdp"
    return "repair_portal_auth"


def credential_repair_validation_command(item, target_month):
    if not isinstance(item, dict):
        return None
    downloader_id = item.get("source_downloader_id")
    if downloader_id:
        return (
            "MORTGAGE_DOWNLOADER_PROFILE=credential_check "
            f"MORTGAGE_DOWNLOADER_IDS={downloader_id} "
            f"MORTGAGE_STATEMENT_TARGET_MONTH={target_month} "
            "python3 scripts/run_mortgage_statement_downloaders.py"
        )
    return item.get("validation_command")


def credential_availability_for_repair(item):
    if not isinstance(item, dict):
        return None
    value = item.get("credentials_available")
    if value is not None:
        return value
    if item.get("credential_lookup_status") == "failed":
        return False
    return None


def credential_source_for_repair(item):
    if not isinstance(item, dict):
        return None
    value = item.get("credential_source")
    if value not in (None, "", [], {}):
        return value
    if credential_availability_for_repair(item) is False:
        return "unavailable"
    return None


def manual_auth_reason_for_repair(item):
    if not isinstance(item, dict):
        return None
    value = item.get("manual_auth_reason")
    if value not in (None, "", [], {}):
        return value
    if credential_availability_for_repair(item) is False:
        source_text = " ".join(
            str(item.get(key) or "")
            for key in (
                "source_downloader_id",
                "credential_lookup_uri_host",
                "credential_lookup_item_name",
                "servicer_hint",
                "servicer",
            )
        ).lower()
        if "loandepot" in source_text:
            return "loandepot_credentials_unavailable"
        return "credentials_unavailable"
    return None


def portal_auth_repair_validation_command(item, target_month):
    if not isinstance(item, dict):
        return None
    downloader_id = item.get("source_downloader_id")
    if downloader_id:
        return (
            "MORTGAGE_DOWNLOADER_PROFILE=live_cdp "
            f"MORTGAGE_DOWNLOADER_IDS={downloader_id} "
            f"MORTGAGE_STATEMENT_TARGET_MONTH={target_month} "
            "python3 scripts/run_mortgage_statement_downloaders.py"
        )
    return item.get("live_cdp_downloader_command") or item.get("validation_command")


def credential_repair_plan(actions, target_month):
    if not isinstance(actions, list):
        return []
    plan = []
    seen = set()
    for item in actions:
        if not isinstance(item, dict):
            continue
        repair_action = derive_credential_repair_action(item)
        if not repair_action:
            continue
        row = {
            "property": item.get("property"),
            "reason": item.get("reason"),
            "command_precondition_status": item.get("command_precondition_status"),
            "command_precondition_reason": item.get("command_precondition_reason"),
            "credentials_available": credential_availability_for_repair(item),
            "credential_source": credential_source_for_repair(item),
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
            "credential_repair_instruction": item.get("credential_repair_instruction"),
            "credential_login_hint_mismatch": item.get("credential_login_hint_mismatch"),
            "credential_login_hint_mismatch_overridden": item.get("credential_login_hint_mismatch_overridden"),
            "manual_auth_reason": manual_auth_reason_for_repair(item),
            "credential_repair_action": repair_action,
            "credential_repair_validation_command": credential_repair_validation_command(item, target_month),
            "operator_next_action": (
                item.get("source_downloader_operator_next_action")
                or item.get("source_downloader_suggested_next_action")
                or item.get("command_precondition_next_action")
            ),
            "source_downloader_id": item.get("source_downloader_id"),
        }
        row = {
            key: value
            for key, value in row.items()
            if value not in (None, "", {}) and (value != [] or key == "credential_lookup_candidate_items")
        }
        marker = json.dumps(
            {
                "property": row.get("property"),
                "credential_repair_action": row.get("credential_repair_action"),
                "credential_lookup_item_name": row.get("credential_lookup_item_name"),
            },
            sort_keys=True,
        )
        if marker in seen:
            continue
        seen.add(marker)
        plan.append(row)
    return plan


def portal_auth_repair_plan(actions, target_month):
    if not isinstance(actions, list):
        return []
    plan = []
    seen = set()
    for item in actions:
        if not isinstance(item, dict):
            continue
        repair_action = derive_portal_auth_repair_action(item)
        if not repair_action:
            continue
        row = {
            "property": item.get("property"),
            "reason": item.get("reason"),
            "command_precondition_status": item.get("command_precondition_status"),
            "command_precondition_reason": item.get("command_precondition_reason"),
            "capture_auth_blocker_reason": item.get("capture_auth_blocker_reason"),
            "capture_command_blocked_reason": item.get("capture_command_blocked_reason"),
            "auth_issue": item.get("auth_issue"),
            "manual_auth_reason": item.get("manual_auth_reason"),
            "auth_failure_reason": item.get("auth_failure_reason"),
            "auth_failure_visible_reason": item.get("auth_failure_visible_reason"),
            "credential_lookup_status": item.get("credential_lookup_status"),
            "credential_lookup_item_name": item.get("credential_lookup_item_name"),
            "credential_lookup_expected_folder_name": item.get("credential_lookup_expected_folder_name"),
            "credential_lookup_uri_host": item.get("credential_lookup_uri_host"),
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
            "portal_auth_repair_action": repair_action,
            "portal_auth_repair_validation_command": portal_auth_repair_validation_command(item, target_month),
            "operator_next_action": (
                item.get("source_downloader_operator_next_action")
                or item.get("source_downloader_suggested_next_action")
                or item.get("command_precondition_next_action")
                or item.get("capture_auth_next_action")
            ),
            "source_downloader_id": item.get("source_downloader_id"),
        }
        row = {
            key: value
            for key, value in row.items()
            if value not in (None, "", {}) and (value != [] or key == "credential_lookup_candidate_items")
        }
        marker = json.dumps(
            {
                "property": row.get("property"),
                "portal_auth_repair_action": row.get("portal_auth_repair_action"),
                "source_downloader_id": row.get("source_downloader_id"),
            },
            sort_keys=True,
        )
        if marker in seen:
            continue
        seen.add(marker)
        plan.append(row)
    return plan


def live_cdp_downloader_handoffs(*collections):
    rows = []
    seen = set()
    for collection in collections:
        if not isinstance(collection, list):
            continue
        for item in collection:
            if not isinstance(item, dict):
                continue
            command = item.get("live_cdp_downloader_command")
            ready = item.get("live_cdp_downloader_ready_after_visible_auth")
            if not command and ready is not True:
                continue
            row = {
                "property": item.get("property"),
                "next_action": item.get("next_action"),
                "capture_auth_blocker": item.get("capture_auth_blocker"),
                "capture_auth_blocker_reason": item.get("capture_auth_blocker_reason"),
                "capture_auth_next_action": item.get("capture_auth_next_action"),
                "capture_command_ready_to_run_now": item.get("capture_command_ready_to_run_now"),
                "capture_command_blocked_reason": item.get("capture_command_blocked_reason"),
                "auth_issue": item.get("auth_issue"),
                "manual_auth_reason": item.get("manual_auth_reason"),
                "auth_failure_reason": item.get("auth_failure_reason"),
                "auth_failure_visible_reason": item.get("auth_failure_visible_reason"),
                "command_precondition_status": item.get("command_precondition_status"),
                "command_precondition_next_action": item.get("command_precondition_next_action"),
                "live_cdp_downloader_command": command,
                "live_cdp_downloader_ready_after_visible_auth": ready,
                "source_downloader_id": item.get("source_downloader_id"),
                "source_downloader_operator_next_action": item.get("source_downloader_operator_next_action"),
                "source_downloader_suggested_next_action": item.get("source_downloader_suggested_next_action"),
            }
            marker = json.dumps(
                {
                    "property": row.get("property"),
                    "live_cdp_downloader_command": row.get("live_cdp_downloader_command"),
                },
                sort_keys=True,
            )
            if marker in seen:
                continue
            seen.add(marker)
            rows.append(row)
    return rows


def merge_capture_queue_auth_context(report, *plan_keys):
    def norm(value):
        return " ".join(str(value or "").strip().casefold().split())

    context_by_property = {}
    stale_auth_reason_values = {
        "authenticated_browser_required",
        "password_reset_in_progress",
    }
    context_fields = [
        "capture_auth_blocker",
        "capture_auth_blocker_reason",
        "capture_auth_next_action",
        "capture_command_ready_to_run_now",
        "capture_command_blocked_reason",
        "auth_issue",
        "manual_auth_reason",
        "auth_failure_reason",
        "auth_failure_visible_reason",
        "auth_state",
        "auto_login_attempted",
        "auto_login_status",
        "auto_login_blocked_reason",
        "auto_login_step",
        "live_cdp_downloader_command",
        "live_cdp_downloader_ready_after_visible_auth",
        "source_downloader_id",
        "source_downloader_auth_issue",
        "source_downloader_manual_auth_reason",
        "source_downloader_operator_next_action",
        "source_downloader_suggested_next_action",
    ]
    for source_key in (
        "mortgage_workflow_capture_queue_entries",
        "mortgage_workflow_capture_queue_actions",
        "mortgage_workflow_capture_queue_processor_operator_next_actions",
        "mortgage_workflow_capture_queue_processor_existing_har_recapture_requirements",
        "mortgage_workflow_capture_queue_processor_existing_har_target_month_pdf_recapture_requirements",
        "mortgage_workflow_capture_queue_processor_existing_body_recapture_requirements",
        "mortgage_workflow_capture_queue_processor_results",
    ):
        source_items = report.get(source_key)
        if not isinstance(source_items, list):
            continue
        for source_item in source_items:
            if not isinstance(source_item, dict):
                continue
            prop_key = norm(source_item.get("property"))
            if not prop_key:
                continue
            target = context_by_property.setdefault(prop_key, {})
            for field in context_fields:
                value = source_item.get(field)
                if value not in (None, "", [], {}):
                    target[field] = value
            if source_item.get("capture_auth_blocker") is True:
                target["capture_auth_blocker"] = True
                target["capture_command_ready_to_run_now"] = False
                if source_item.get("capture_auth_blocker_reason") and not target.get("capture_command_blocked_reason"):
                    target["capture_command_blocked_reason"] = source_item.get("capture_auth_blocker_reason")

    for plan_key in plan_keys:
        plan = report.get(plan_key)
        if not isinstance(plan, list):
            continue
        merged_count = 0
        for item in plan:
            if not isinstance(item, dict):
                continue
            auth_context = context_by_property.get(norm(item.get("property")))
            if not auth_context:
                continue
            merged = False
            for field, value in auth_context.items():
                if field == "capture_command_ready_to_run_now" and auth_context.get("capture_auth_blocker") is True:
                    if item.get(field) is not False:
                        item[field] = False
                        merged = True
                    continue
                if item.get(field) in (None, "", [], {}) and value not in (None, "", [], {}):
                    item[field] = value
                    merged = True
            if auth_context.get("capture_auth_blocker") is True:
                blocker_reason = auth_context.get("capture_auth_blocker_reason")
                blocker_next_action = (
                    auth_context.get("source_downloader_operator_next_action")
                    or auth_context.get("source_downloader_suggested_next_action")
                    or auth_context.get("capture_auth_next_action")
                )
                if item.get("capture_auth_blocker") is not True:
                    item["capture_auth_blocker"] = True
                    merged = True
                if not item.get("capture_command_blocked_reason") and blocker_reason:
                    item["capture_command_blocked_reason"] = blocker_reason
                    merged = True
                if (
                    blocker_reason
                    and (
                        item.get("type") == "resolve_configured_downloader_blocker"
                        or item.get("reason") in stale_auth_reason_values
                    )
                    and item.get("reason") != blocker_reason
                ):
                    previous_reason = item.get("reason")
                    item["reason"] = blocker_reason
                    reasons = item.get("reasons") if isinstance(item.get("reasons"), list) else []
                    updated_reasons = [blocker_reason]
                    for reason in [previous_reason, *reasons]:
                        if reason not in (None, "", [], {}) and reason not in updated_reasons:
                            updated_reasons.append(reason)
                    item["reasons"] = updated_reasons
                    merged = True
                if item.get("command_precondition_status") == "ready":
                    item["command_precondition_status"] = "blocked_by_auth"
                    merged = True
                if blocker_reason and not item.get("command_precondition_blocker"):
                    item["command_precondition_blocker"] = blocker_reason
                    merged = True
                if blocker_reason and not item.get("command_precondition_reason"):
                    item["command_precondition_reason"] = blocker_reason
                    merged = True
                if blocker_next_action and not item.get("command_precondition_next_action"):
                    item["command_precondition_next_action"] = blocker_next_action
                    merged = True
                stale_next_actions = {
                    "authenticate_visible_loandepot_tab_then_run_live_cdp",
                    "capture_target_month_statement",
                    "capture_target_month_statement_pdf",
                    "recapture_target_month_statement_pdf",
                }
                if (
                    blocker_next_action
                    and item.get("suggested_next_action") in (None, "", [], {}, *stale_next_actions)
                    and item.get("suggested_next_action") != blocker_next_action
                ):
                    item["suggested_next_action"] = blocker_next_action
                    merged = True
                if (
                    blocker_next_action
                    and item.get("next_action") in (None, "", [], {}, *stale_next_actions)
                    and item.get("next_action") != blocker_next_action
                ):
                    item["next_action"] = blocker_next_action
                    merged = True
                if item.get("command_precondition_safe_to_capture_now") is not False:
                    item["command_precondition_safe_to_capture_now"] = False
                    merged = True
            if merged:
                merged_count += 1
        report[f"{plan_key}_capture_auth_context_merged_count"] = merged_count
report = {
    "job": "baselane-monthly-mortgage-workflow-gate",
    "status": os.environ["BASELANE_MORTGAGE_GATE_STATUS"],
    "reason": os.environ.get("BASELANE_MORTGAGE_GATE_REASON") or None,
    "stamp": os.environ["BASELANE_MORTGAGE_GATE_STAMP"],
    "mortgage_statement_target_month": os.environ.get("BASELANE_MORTGAGE_STATEMENT_TARGET_MONTH"),
    "mortgage_statement_target_month_kind": "statement_month",
    "mortgage_statement_target_month_basis": (
        "Servicer downloader and tokenomics evidence month. Monthly payment-month balances may be derived "
        "from this statement month through coownership_mortgage_amortization_terms.json."
    ),
    "mortgage_downloader_exact_target_month_mode": os.environ.get("BASELANE_MORTGAGE_DOWNLOADER_EXACT_TARGET_MONTH_MODE"),
    "mortgage_workflow_month_semantics": {
        "stamp": os.environ["BASELANE_MORTGAGE_GATE_STAMP"],
        "statement_target_month": os.environ.get("BASELANE_MORTGAGE_STATEMENT_TARGET_MONTH"),
        "citadel_statement_target_month": os.environ.get("BASELANE_MORTGAGE_CITADEL_STATEMENT_TARGET_MONTH"),
        "statement_target_month_kind": "statement_month",
        "downloader_exact_target_month_mode": os.environ.get("BASELANE_MORTGAGE_DOWNLOADER_EXACT_TARGET_MONTH_MODE"),
        "tokenomics_payment_month_source": "coownership_mortgage_amortization_terms.json anchor_payment_month/payment_number",
        "notes": (
            "The statement target month is the statement evidence being downloaded or verified. "
            "For co-owner-paid mortgages, the workbook may write balances for the payment month implied by "
            "the amortization terms, so a prior statement month can still feed the current payment cycle."
        ),
    },
    "citadel_statement_target_month": os.environ.get("BASELANE_MORTGAGE_CITADEL_STATEMENT_TARGET_MONTH"),
    "state_file": os.environ["BASELANE_MORTGAGE_GATE_STATE_FILE"],
    "report_only": os.environ.get("BASELANE_MORTGAGE_GATE_REPORT_ONLY") == "1",
    "downloader_rc": int(os.environ.get("BASELANE_MORTGAGE_GATE_DOWNLOADER_RC") or 0),
    "citadel_download_rc": int(os.environ.get("BASELANE_MORTGAGE_GATE_CITADEL_RC") or 0),
    "mortgage_splits_rc": int(os.environ.get("BASELANE_MORTGAGE_GATE_SPLITS_RC") or 0),
    "mortgage_splits_report": os.environ.get("BASELANE_MORTGAGE_SPLITS_REPORT"),
    "no_mortgage_financials_guard_rc": int(os.environ.get("BASELANE_NO_MORTGAGE_FINANCIALS_GUARD_RC") or 0),
    "no_mortgage_financials_guard_report": os.environ.get("BASELANE_NO_MORTGAGE_FINANCIALS_GUARD_REPORT"),
    "mortgage_archive_restore_rc": int(os.environ.get("BASELANE_MORTGAGE_ARCHIVE_RESTORE_RC") or 0),
    "mortgage_archive_restore_report": os.environ.get("BASELANE_MORTGAGE_ARCHIVE_RESTORE_REPORT"),
    "mortgage_archive_restore_markdown": os.environ.get("BASELANE_MORTGAGE_ARCHIVE_RESTORE_MARKDOWN"),
    "tokenomics_rc": int(os.environ.get("BASELANE_MORTGAGE_GATE_TOKENOMICS_RC") or 0),
    "mortgage_downloader_coverage_rc": int(os.environ.get("BASELANE_MORTGAGE_GATE_COVERAGE_RC") or 0),
    "mortgage_workflow_evidence_advance_report": os.environ.get("BASELANE_MORTGAGE_EVIDENCE_ADVANCE_REPORT"),
    "mortgage_workflow_evidence_advance_rc": int(os.environ.get("BASELANE_MORTGAGE_EVIDENCE_ADVANCE_RC") or 0),
    "mortgage_workflow_replayability_rc": int(os.environ.get("BASELANE_MORTGAGE_REPLAYABILITY_RC") or 0),
    "tokenomics_report": os.environ.get("BASELANE_MORTGAGE_TOKENOMICS_REPORT"),
    "tokenomics_workbook_write_guard_status": os.environ.get("BASELANE_MORTGAGE_TOKENOMICS_WRITE_GUARD_STATUS") or "not_evaluated",
    "tokenomics_workbook_write_guard_reason": os.environ.get("BASELANE_MORTGAGE_TOKENOMICS_WRITE_GUARD_REASON") or None,
    "mortgage_downloader_tokenomics_blocking_gap_count": int(os.environ.get("BASELANE_MORTGAGE_DOWNLOADER_TOKENOMICS_BLOCKING_GAP_COUNT") or 0),
    "mortgage_downloader_tokenomics_blocking_gap_properties": env_json("BASELANE_MORTGAGE_DOWNLOADER_TOKENOMICS_BLOCKING_GAP_PROPERTIES", []),
    "mortgage_downloader_citadel_tokenomics_blocking": os.environ.get("BASELANE_MORTGAGE_CITADEL_TOKENOMICS_BLOCKING") == "true",
    "mortgage_workflow_capture_queue_tokenomics_blocking_count": int(os.environ.get("BASELANE_MORTGAGE_CAPTURE_QUEUE_TOKENOMICS_BLOCKING_COUNT") or 0),
    "mortgage_workflow_capture_queue_tokenomics_blocking_properties": env_json("BASELANE_MORTGAGE_CAPTURE_QUEUE_TOKENOMICS_BLOCKING_PROPERTIES", []),
    "mortgage_workflow_capture_queue_tokenomics_non_blocking_count": int(os.environ.get("BASELANE_MORTGAGE_CAPTURE_QUEUE_TOKENOMICS_NON_BLOCKING_COUNT") or 0),
    "mortgage_workflow_capture_queue_tokenomics_non_blocking_properties": env_json("BASELANE_MORTGAGE_CAPTURE_QUEUE_TOKENOMICS_NON_BLOCKING_PROPERTIES", []),
    "tokenomics_truth_ready": os.environ.get("BASELANE_MORTGAGE_TOKENOMICS_TRUTH_READY") == "true",
    "tokenomics_infra_review_ignored_for_workbook_write": os.environ.get("BASELANE_MORTGAGE_TOKENOMICS_INFRA_REVIEW_IGNORED") == "true",
    "mortgage_workflow_capture_queue_tokenomics_workbook_write_blocker_count": None,
    "mortgage_workflow_capture_queue_tokenomics_workbook_write_blocker_properties": None,
    "mortgage_workflow_capture_queue_tokenomics_workbook_write_blocker_reasons": None,
    "mortgage_downloader_coverage_report": os.environ.get("BASELANE_MORTGAGE_COVERAGE_REPORT"),
    "mortgage_workflow_evidence_refresh_report": os.environ.get("BASELANE_MORTGAGE_EVIDENCE_REFRESH_REPORT"),
    "mortgage_workflow_evidence_refresh_rc": int(os.environ.get("BASELANE_MORTGAGE_EVIDENCE_REFRESH_RC") or 0),
    "mortgage_workflow_replayability_report": os.environ.get("BASELANE_MORTGAGE_REPLAYABILITY_REPORT"),
    "mortgage_workflow_capture_queue_report": os.environ.get("BASELANE_MORTGAGE_CAPTURE_QUEUE_REPORT"),
    "mortgage_workflow_capture_queue_markdown": os.environ.get("BASELANE_MORTGAGE_CAPTURE_QUEUE_MARKDOWN"),
    "mortgage_workflow_capture_queue_rc": int(os.environ.get("BASELANE_MORTGAGE_CAPTURE_QUEUE_RC") or 0),
    "mortgage_workflow_capture_queue_processor_report": os.environ.get("BASELANE_MORTGAGE_CAPTURE_QUEUE_PROCESSOR_REPORT"),
    "mortgage_workflow_capture_queue_processor_markdown": os.environ.get("BASELANE_MORTGAGE_CAPTURE_QUEUE_PROCESSOR_MARKDOWN"),
    "mortgage_workflow_capture_queue_processor_rc": int(os.environ.get("BASELANE_MORTGAGE_CAPTURE_QUEUE_PROCESSOR_RC") or 0),
    "mortgage_downloader_report": os.environ.get("BASELANE_MORTGAGE_DOWNLOADER_REPORT"),
    "mortgage_downloader_workflow_timeout_seconds": int(os.environ.get("BASELANE_MORTGAGE_DOWNLOADER_WORKFLOW_TIMEOUT_SECONDS") or 0),
    "mortgage_workflow_step_timeout_seconds": int(os.environ.get("BASELANE_MORTGAGE_WORKFLOW_STEP_TIMEOUT_SECONDS") or 0),
    "mortgage_workflow_step_kill_after_seconds": int(os.environ.get("BASELANE_MORTGAGE_WORKFLOW_STEP_KILL_AFTER_SECONDS") or 0),
    "mortgage_downloader_credential_refresh_report": os.environ.get("BASELANE_MORTGAGE_CREDENTIAL_REFRESH_REPORT"),
    "mortgage_downloader_credential_refresh_rc": int(os.environ.get("BASELANE_MORTGAGE_CREDENTIAL_REFRESH_RC") or 0),
    "mortgage_downloader_credential_refresh_status": os.environ.get("BASELANE_MORTGAGE_CREDENTIAL_REFRESH_STATUS") or "skipped",
    "mortgage_downloader_credential_refresh_profile": os.environ.get("BASELANE_MORTGAGE_CREDENTIAL_REFRESH_PROFILE") or None,
    "mortgage_downloader_credential_refresh_ids": [
        item.strip()
        for item in (os.environ.get("BASELANE_MORTGAGE_CREDENTIAL_REFRESH_IDS") or "").replace(",", " ").split()
        if item.strip()
    ],
    "mortgage_downloader_credential_refresh_timeout_seconds": int(os.environ.get("BASELANE_MORTGAGE_CREDENTIAL_REFRESH_TIMEOUT_SECONDS") or 0),
    "mortgage_downloader_citadel_har_preflight_report": os.environ.get("BASELANE_MORTGAGE_CITADEL_HAR_DIAGNOSTICS_REPORT"),
    "mortgage_downloader_citadel_capture_plan_report": os.environ.get("BASELANE_MORTGAGE_CITADEL_CAPTURE_PLAN_REPORT"),
    "mortgage_downloader_citadel_capture_plan_markdown": os.environ.get("BASELANE_MORTGAGE_CITADEL_CAPTURE_PLAN_MARKDOWN"),
    "mortgage_downloader_citadel_capture_manual_auth_fallback_file": os.environ.get("BASELANE_MORTGAGE_CITADEL_CAPTURE_MANUAL_AUTH_FILE"),
    "mortgage_downloader_citadel_har_preflight_rc": int(os.environ.get("BASELANE_MORTGAGE_CITADEL_HAR_DIAGNOSTICS_RC") or 0),
    "mortgage_downloader_citadel_auth_preflight_report": os.environ.get("BASELANE_MORTGAGE_CITADEL_AUTH_PREFLIGHT_REPORT"),
    "mortgage_downloader_citadel_auth_preflight_rc": int(os.environ.get("BASELANE_MORTGAGE_CITADEL_AUTH_PREFLIGHT_RC") or 0),
    "review_packet": os.environ.get("BASELANE_MORTGAGE_REVIEW_PACKET"),
    "repair_packet": os.environ.get("BASELANE_MORTGAGE_REPAIR_PACKET"),
    "repair_packet_markdown": os.environ.get("BASELANE_MORTGAGE_REPAIR_PACKET_MARKDOWN"),
}
if report["report_only"]:
    report_only_tokenomics_live = os.environ.get("BASELANE_MORTGAGE_REPORT_ONLY_TOKENOMICS_WRITE_LIVE") == "1"
    report["mortgage_downloader_report_only_summarized_existing_reports"] = os.environ.get("BASELANE_MORTGAGE_DOWNLOADER_REPORT_ONLY_SUMMARIZED") == "1"
    report["live_stages_skipped"] = [
        "citadel-auth-preflight",
        "mortgage-statement-downloaders",
        "baselane-mortgage-splits",
    ]
    if not report_only_tokenomics_live:
        report["live_stages_skipped"].append("coownership-tokenomics-workbook-write")
    if report["mortgage_downloader_report_only_summarized_existing_reports"]:
        report["mortgage_downloader_execution_status"] = "summarized_existing_reports"
    else:
        report["mortgage_downloader_execution_status"] = "skipped_report_only"
    report["mortgage_downloader_report_is_live_for_this_run"] = False
    report["tokenomics_workbook_write_is_live_for_this_run"] = report_only_tokenomics_live
elif report.get("tokenomics_workbook_write_guard_status") == "blocked":
    report["live_stages_skipped"] = ["coownership-tokenomics-workbook-write"]
    report["mortgage_downloader_execution_status"] = "attempted" if report["downloader_rc"] is not None else "unknown"
    report["mortgage_downloader_report_is_live_for_this_run"] = True
    report["tokenomics_workbook_write_is_live_for_this_run"] = False
else:
    report["live_stages_skipped"] = []
    report["mortgage_downloader_execution_status"] = "attempted" if report["downloader_rc"] is not None else "unknown"
    report["mortgage_downloader_report_is_live_for_this_run"] = True
    report["tokenomics_workbook_write_is_live_for_this_run"] = True
splits_report = report.get("mortgage_splits_report")
if splits_report and os.path.exists(splits_report):
    try:
        with open(splits_report, encoding="utf-8") as f:
            splits_data = json.load(f)
        if isinstance(splits_data, dict):
            report["mortgage_splits_status"] = splits_data.get("status")
            report["mortgage_splits_classification"] = splits_data.get("classification")
            report["mortgage_splits_issue_count"] = splits_data.get("issue_count")
            report["mortgage_splits_ok_count"] = splits_data.get("ok_count")
            report["mortgage_splits_safe_to_run_automatically"] = splits_data.get("safe_to_run_automatically")
            report["mortgage_splits_property_id_count"] = splits_data.get("property_id_count")
            report["mortgage_splits_tag_id_count"] = splits_data.get("tag_id_count")
            report["mortgage_splits_review_command"] = splits_data.get("review_command")
            report["mortgage_splits_review_command_valid"] = splits_data.get("valid_review_command_count", 0) > 0
    except Exception as exc:
        report["mortgage_splits_report_error"] = str(exc)
no_mortgage_guard_report = report.get("no_mortgage_financials_guard_report")
if no_mortgage_guard_report and os.path.exists(no_mortgage_guard_report):
    try:
        with open(no_mortgage_guard_report, encoding="utf-8") as f:
            no_mortgage_guard_data = json.load(f)
        if isinstance(no_mortgage_guard_data, dict):
            report["no_mortgage_financials_guard_status"] = no_mortgage_guard_data.get("status")
            report["no_mortgage_financials_guard_apply"] = no_mortgage_guard_data.get("apply")
            report["no_mortgage_financials_guard_file_count"] = no_mortgage_guard_data.get("file_count")
            report["no_mortgage_financials_guard_changed_file_count"] = no_mortgage_guard_data.get("changed_file_count")
            report["no_mortgage_financials_guard_change_count"] = no_mortgage_guard_data.get("change_count")
            report["no_mortgage_financials_guard_remaining_nonzero_count"] = no_mortgage_guard_data.get("remaining_nonzero_count")
            report["no_mortgage_financials_guard_read_error_count"] = no_mortgage_guard_data.get("read_error_count")
    except Exception as exc:
        report["no_mortgage_financials_guard_report_error"] = str(exc)
archive_restore_report = report.get("mortgage_archive_restore_report")
if archive_restore_report and os.path.exists(archive_restore_report):
    try:
        with open(archive_restore_report, encoding="utf-8") as f:
            archive_restore_data = json.load(f)
        report["mortgage_archive_restore_status"] = archive_restore_data.get("status")
        report["mortgage_archive_restore_reason"] = archive_restore_data.get("reason")
        report["mortgage_archive_restore_apply"] = archive_restore_data.get("apply")
        report["mortgage_archive_restore_target_month"] = archive_restore_data.get("target_month")
        report["mortgage_archive_restore_current_month_only"] = archive_restore_data.get("current_month_only")
        report["mortgage_archive_restore_copy_candidate_count"] = archive_restore_data.get("copy_candidate_count")
        report["mortgage_archive_restore_copied_count"] = archive_restore_data.get("copied_count")
        report["mortgage_archive_restore_conflict_count"] = archive_restore_data.get("conflict_count")
        report["mortgage_archive_restore_apply_error_count"] = archive_restore_data.get("apply_error_count")
        report["mortgage_archive_restore_remaining_copy_candidate_count"] = archive_restore_data.get("remaining_copy_candidate_count")
        report["mortgage_archive_restore_safe_to_run_automatically"] = archive_restore_data.get("safe_to_run_automatically")
        report["mortgage_archive_restore_idempotent_replay_safe"] = archive_restore_data.get("idempotent_replay_safe")
        report["mortgage_archive_restore_copy_plan_safe_to_apply_automatically"] = archive_restore_data.get("copy_plan_safe_to_apply_automatically")
        plans = archive_restore_data.get("property_plans")
        if isinstance(plans, list):
            report["mortgage_archive_restore_property_count"] = len(plans)
            report["mortgage_archive_restore_properties_with_candidates"] = [
                item.get("property")
                for item in plans
                if isinstance(item, dict) and int(item.get("copy_candidate_count") or 0) > 0
            ]
            report["mortgage_archive_restore_stale_skip_properties"] = [
                item.get("property")
                for item in plans
                if isinstance(item, dict)
                and any(
                    isinstance(entry, dict) and entry.get("reason") == "not_current_month_statement"
                    for entry in (item.get("entries") or [])
                )
            ]
    except Exception as exc:
        report["mortgage_archive_restore_status"] = "unreadable"
        report["mortgage_archive_restore_error"] = str(exc)
har_preflight_data = {}
har_preflight_next_action = {}
har_preflight_report = report.get("mortgage_downloader_citadel_har_preflight_report")
if har_preflight_report and os.path.exists(har_preflight_report):
    try:
        with open(har_preflight_report, encoding="utf-8") as f:
            har_data = json.load(f)
        har_preflight_data = har_data if isinstance(har_data, dict) else {}
        har_preflight_next_action = har_preflight_data.get("next_action")
        if not isinstance(har_preflight_next_action, dict):
            har_preflight_next_action = {}
        report["mortgage_downloader_citadel_har_preflight_status"] = har_data.get("status")
        report["mortgage_downloader_citadel_har_preflight_capture_quality_status"] = har_data.get("capture_quality_status")
        report["mortgage_downloader_citadel_har_preflight_replay_blocker"] = har_data.get("replay_blocker")
        report["mortgage_downloader_citadel_har_preflight_missing_response_body_count"] = har_data.get("missing_response_body_count")
        report["mortgage_downloader_citadel_har_preflight_missing_response_body_paths"] = har_data.get("missing_response_body_paths")
        report["mortgage_downloader_citadel_har_preflight_missing_response_body_path_counts"] = har_data.get("missing_response_body_path_counts")
        report["mortgage_downloader_citadel_har_preflight_response_body_requirements"] = har_data.get("response_body_requirements")
        report["mortgage_downloader_citadel_har_preflight_replayable_document_payload_count"] = har_data.get("replayable_document_payload_count")
        report["mortgage_downloader_citadel_har_preflight_target_month"] = har_data.get("target_month")
        report["mortgage_downloader_citadel_har_preflight_target_month_replayable_document_available"] = har_data.get("target_month_replayable_document_available")
        report["mortgage_downloader_citadel_har_preflight_target_month_replayable_document_payload_count"] = har_data.get("target_month_replayable_document_payload_count")
        report["mortgage_downloader_citadel_har_preflight_replayable_statement_months"] = har_data.get("replayable_statement_months")
        report["mortgage_downloader_citadel_har_preflight_statement_document_months"] = har_data.get("statement_document_months")
        report["mortgage_downloader_citadel_har_preflight_direct_pdf_response_count"] = har_data.get("direct_pdf_response_count")
        report["mortgage_downloader_citadel_har_preflight_source_direct_pdf_candidate_count"] = har_data.get("source_direct_pdf_candidate_count")
        report["mortgage_downloader_citadel_har_preflight_source_direct_pdf_path_counts"] = har_data.get("source_direct_pdf_path_counts")
        report["mortgage_downloader_citadel_har_preflight_source_direct_pdf_filenames"] = har_data.get("source_direct_pdf_filenames")
        report["mortgage_downloader_citadel_har_preflight_source_direct_pdf_filename_candidates"] = har_data.get("source_direct_pdf_filename_candidates")
        report["mortgage_downloader_citadel_har_preflight_target_month_direct_pdf_filenames"] = har_data.get("target_month_direct_pdf_filenames")
        report["mortgage_downloader_citadel_har_preflight_target_month_direct_pdf_filename_candidates"] = har_data.get("target_month_direct_pdf_filename_candidates")
        report["mortgage_downloader_citadel_har_preflight_target_month_direct_pdf_body_missing_candidate_count"] = har_data.get("target_month_direct_pdf_body_missing_candidate_count")
        report["mortgage_downloader_citadel_har_preflight_source_required_response_candidate_count"] = har_data.get("source_required_response_candidate_count")
        report["mortgage_downloader_citadel_har_preflight_source_required_response_path_counts"] = har_data.get("source_required_response_path_counts")
        report["mortgage_downloader_citadel_har_preflight_direct_pdf_missing_response_count"] = har_data.get("direct_pdf_missing_response_count")
        report["mortgage_downloader_citadel_har_preflight_direct_pdf_missing_response_paths"] = har_data.get("direct_pdf_missing_response_paths")
        report["mortgage_downloader_citadel_har_preflight_replayable_json_response_count"] = har_data.get("replayable_json_response_count")
        report["mortgage_downloader_citadel_har_preflight_embedded_response_body_count"] = har_data.get("embedded_response_body_count")
        report["mortgage_downloader_citadel_har_preflight_embedded_access_token_count"] = har_data.get("embedded_access_token_count")
        report["mortgage_downloader_citadel_har_preflight_can_replay_documents"] = har_data.get("can_replay_documents")
        report["mortgage_downloader_citadel_har_preflight_next_action_status"] = har_preflight_next_action.get("status")
        report["mortgage_downloader_citadel_har_preflight_next_action_reason"] = har_preflight_next_action.get("reason")
        report["mortgage_downloader_citadel_har_preflight_next_action_command"] = har_preflight_next_action.get("next_command") or har_data.get("next_command")
        report["mortgage_downloader_citadel_har_preflight_next_action_capture_command"] = har_preflight_next_action.get("capture_command")
        report["mortgage_downloader_citadel_har_preflight_next_action_capture_required"] = har_preflight_next_action.get("capture_required")
        report["mortgage_downloader_citadel_har_preflight_next_action_target_month"] = har_preflight_next_action.get("target_month")
        report["mortgage_downloader_citadel_har_preflight_next_action_target_month_replayable_document_available"] = har_preflight_next_action.get("target_month_replayable_document_available")
        report["mortgage_downloader_citadel_har_preflight_next_action_target_month_replayable_document_payload_count"] = har_preflight_next_action.get("target_month_replayable_document_payload_count")
        report["mortgage_downloader_citadel_har_preflight_next_action_replayable_statement_months"] = har_preflight_next_action.get("replayable_statement_months")
        report["mortgage_downloader_citadel_har_preflight_next_action_statement_document_months"] = har_preflight_next_action.get("statement_document_months")
        report["mortgage_downloader_citadel_har_preflight_next_action_required_response_paths"] = har_preflight_next_action.get("required_response_paths")
        report["mortgage_downloader_citadel_har_preflight_next_action_response_body_requirements"] = har_preflight_next_action.get("response_body_requirements")
        report["mortgage_downloader_citadel_har_preflight_next_action_source_direct_pdf_filenames"] = har_preflight_next_action.get("source_direct_pdf_filenames")
        report["mortgage_downloader_citadel_har_preflight_next_action_target_month_direct_pdf_filenames"] = har_preflight_next_action.get("target_month_direct_pdf_filenames")
        report["mortgage_downloader_citadel_har_preflight_next_action_target_month_direct_pdf_body_missing_candidate_count"] = har_preflight_next_action.get("target_month_direct_pdf_body_missing_candidate_count")
        report["mortgage_downloader_citadel_har_preflight_next_action_source_required_response_candidate_count"] = har_preflight_next_action.get("source_required_response_candidate_count")
        report["mortgage_downloader_citadel_har_preflight_next_action_source_required_response_path_counts"] = har_preflight_next_action.get("source_required_response_path_counts")
        report["mortgage_downloader_citadel_har_preflight_next_action_install_verified_capture_report"] = har_preflight_next_action.get("install_verified_capture_report")
        report["mortgage_downloader_citadel_har_preflight_next_action_install_verified_capture_dry_run_command"] = har_preflight_next_action.get("install_verified_capture_dry_run_command")
        report["mortgage_downloader_citadel_har_preflight_next_action_install_verified_capture_apply_command"] = har_preflight_next_action.get("install_verified_capture_apply_command")
        report["mortgage_downloader_citadel_har_preflight_next_action_install_verified_capture_direct_dry_run_command"] = har_preflight_next_action.get("install_verified_capture_direct_dry_run_command")
        report["mortgage_downloader_citadel_har_preflight_next_action_install_verified_capture_direct_apply_command"] = har_preflight_next_action.get("install_verified_capture_direct_apply_command")
    except Exception as exc:
        report["mortgage_downloader_citadel_har_preflight_status"] = "unreadable"
        report["mortgage_downloader_citadel_har_preflight_error"] = str(exc)
capture_plan_report = report.get("mortgage_downloader_citadel_capture_plan_report")
if capture_plan_report and os.path.exists(capture_plan_report):
    try:
        with open(capture_plan_report, encoding="utf-8") as f:
            capture_plan_data = json.load(f)
        report["mortgage_downloader_citadel_capture_plan_status"] = capture_plan_data.get("status")
        report["mortgage_downloader_citadel_capture_plan_latest_live_auth_report"] = capture_plan_data.get("latest_live_auth_report")
        report["mortgage_downloader_citadel_capture_plan_latest_live_auth_status"] = capture_plan_data.get("latest_live_auth_status")
        report["mortgage_downloader_citadel_capture_plan_latest_live_auth_blocker"] = capture_plan_data.get("latest_live_auth_blocker")
        report["mortgage_downloader_citadel_capture_plan_latest_live_auth_auth_failure_reason"] = capture_plan_data.get("latest_live_auth_auth_failure_reason")
        report["mortgage_downloader_citadel_capture_plan_latest_live_auth_auth_failure_visible_reason"] = capture_plan_data.get("latest_live_auth_auth_failure_visible_reason")
        report["mortgage_downloader_citadel_capture_plan_latest_live_auth_manual_auth_required"] = capture_plan_data.get("latest_live_auth_manual_auth_required")
        report["mortgage_downloader_citadel_capture_plan_latest_live_auth_manual_auth_file"] = capture_plan_data.get("latest_live_auth_manual_auth_file")
        report["mortgage_downloader_citadel_capture_plan_latest_live_auth_oauth_password_grant_error_codes"] = capture_plan_data.get("latest_live_auth_oauth_password_grant_error_codes")
        report["mortgage_downloader_citadel_capture_plan_capture_precondition_status"] = capture_plan_data.get("capture_precondition_status")
        report["mortgage_downloader_citadel_capture_plan_capture_precondition_blocker"] = capture_plan_data.get("capture_precondition_blocker")
        report["mortgage_downloader_citadel_capture_plan_capture_precondition_reason"] = capture_plan_data.get("capture_precondition_reason")
        report["mortgage_downloader_citadel_capture_plan_capture_precondition_manual_auth_required"] = capture_plan_data.get("capture_precondition_manual_auth_required")
        report["mortgage_downloader_citadel_capture_plan_capture_precondition_next_action"] = capture_plan_data.get("capture_precondition_next_action")
        report["mortgage_downloader_citadel_capture_plan_capture_precondition_safe_to_capture_now"] = capture_plan_data.get("capture_precondition_safe_to_capture_now")
        report["mortgage_downloader_citadel_capture_plan_capture_precondition_prepare_command"] = capture_plan_data.get("capture_precondition_prepare_command")
        report["mortgage_downloader_citadel_capture_plan_capture_precondition_safe_to_prepare_now"] = capture_plan_data.get("capture_precondition_safe_to_prepare_now")
        report["mortgage_downloader_citadel_capture_plan_markdown"] = capture_plan_data.get("capture_plan_markdown") or report.get("mortgage_downloader_citadel_capture_plan_markdown")
        report["mortgage_downloader_citadel_capture_plan_target_month"] = capture_plan_data.get("target_month")
        report["mortgage_downloader_citadel_capture_plan_capture_quality_status"] = capture_plan_data.get("capture_quality_status")
        report["mortgage_downloader_citadel_capture_plan_replay_blocker"] = capture_plan_data.get("replay_blocker")
        report["mortgage_downloader_citadel_capture_plan_missing_response_body_count"] = capture_plan_data.get("missing_response_body_count")
        report["mortgage_downloader_citadel_capture_plan_missing_response_body_paths"] = capture_plan_data.get("missing_response_body_paths")
        report["mortgage_downloader_citadel_capture_plan_missing_response_body_path_counts"] = capture_plan_data.get("missing_response_body_path_counts")
        report["mortgage_downloader_citadel_capture_plan_target_month_replayable_document_available"] = capture_plan_data.get("target_month_replayable_document_available")
        report["mortgage_downloader_citadel_capture_plan_target_month_replayable_document_payload_count"] = capture_plan_data.get("target_month_replayable_document_payload_count")
        report["mortgage_downloader_citadel_capture_plan_replayable_statement_months"] = capture_plan_data.get("replayable_statement_months")
        report["mortgage_downloader_citadel_capture_plan_statement_document_months"] = capture_plan_data.get("statement_document_months")
        report["mortgage_downloader_citadel_capture_plan_capture_required"] = capture_plan_data.get("capture_required")
        report["mortgage_downloader_citadel_capture_plan_capture_command"] = capture_plan_data.get("capture_command")
        report["mortgage_downloader_citadel_capture_plan_next_command"] = capture_plan_data.get("next_command")
        report["mortgage_downloader_citadel_capture_plan_capture_har_path"] = capture_plan_data.get("capture_har_path")
        report["mortgage_downloader_citadel_capture_plan_canonical_har_path"] = capture_plan_data.get("canonical_har_path")
        report["mortgage_downloader_citadel_capture_plan_post_capture_diagnostics_command"] = capture_plan_data.get("post_capture_diagnostics_command")
        report["mortgage_downloader_citadel_capture_plan_post_capture_check_script_path"] = capture_plan_data.get("post_capture_check_script_path")
        report["mortgage_downloader_citadel_capture_plan_post_capture_check_command"] = capture_plan_data.get("post_capture_check_command")
        report["mortgage_downloader_citadel_capture_plan_post_capture_check_safe_to_run_after_har_capture"] = capture_plan_data.get("post_capture_check_safe_to_run_after_har_capture")
        report["mortgage_downloader_citadel_capture_plan_validate_existing_capture_command"] = capture_plan_data.get("validate_existing_capture_command")
        report["mortgage_downloader_citadel_capture_plan_advance_capture_workflow_evidence_dry_run_command"] = capture_plan_data.get("advance_capture_workflow_evidence_dry_run_command")
        report["mortgage_downloader_citadel_capture_plan_advance_capture_workflow_evidence_apply_command"] = capture_plan_data.get("advance_capture_workflow_evidence_apply_command")
        report["mortgage_downloader_citadel_capture_plan_install_verified_capture_report"] = capture_plan_data.get("install_verified_capture_report")
        report["mortgage_downloader_citadel_capture_plan_install_verified_capture_dry_run_command"] = capture_plan_data.get("install_verified_capture_dry_run_command")
        report["mortgage_downloader_citadel_capture_plan_install_verified_capture_apply_command"] = capture_plan_data.get("install_verified_capture_apply_command")
        report["mortgage_downloader_citadel_capture_plan_install_verified_capture_direct_dry_run_command"] = capture_plan_data.get("install_verified_capture_direct_dry_run_command")
        report["mortgage_downloader_citadel_capture_plan_install_verified_capture_direct_apply_command"] = capture_plan_data.get("install_verified_capture_direct_apply_command")
        report["mortgage_downloader_citadel_capture_plan_workflow_evidence_advance_report"] = capture_plan_data.get("workflow_evidence_advance_report")
        report["mortgage_downloader_citadel_capture_plan_advance_workflow_evidence_command"] = capture_plan_data.get("advance_workflow_evidence_command")
        report["mortgage_downloader_citadel_capture_plan_advance_workflow_evidence_apply_command"] = capture_plan_data.get("advance_workflow_evidence_apply_command")
        report["mortgage_downloader_citadel_capture_plan_required_response_paths"] = capture_plan_data.get("required_response_paths")
        report["mortgage_downloader_citadel_capture_plan_required_response_path_counts"] = capture_plan_data.get("required_response_path_counts")
        report["mortgage_downloader_citadel_capture_plan_response_body_requirements"] = capture_plan_data.get("response_body_requirements")
        report["mortgage_downloader_citadel_capture_plan_source_direct_pdf_candidate_count"] = capture_plan_data.get("source_direct_pdf_candidate_count")
        report["mortgage_downloader_citadel_capture_plan_source_direct_pdf_path_counts"] = capture_plan_data.get("source_direct_pdf_path_counts")
        report["mortgage_downloader_citadel_capture_plan_source_direct_pdf_filenames"] = capture_plan_data.get("source_direct_pdf_filenames")
        report["mortgage_downloader_citadel_capture_plan_source_direct_pdf_filename_candidates"] = capture_plan_data.get("source_direct_pdf_filename_candidates")
        report["mortgage_downloader_citadel_capture_plan_target_month_direct_pdf_filenames"] = capture_plan_data.get("target_month_direct_pdf_filenames")
        report["mortgage_downloader_citadel_capture_plan_target_month_direct_pdf_filename_candidates"] = capture_plan_data.get("target_month_direct_pdf_filename_candidates")
        report["mortgage_downloader_citadel_capture_plan_target_month_direct_pdf_body_missing_candidate_count"] = capture_plan_data.get("target_month_direct_pdf_body_missing_candidate_count")
        report["mortgage_downloader_citadel_capture_plan_source_required_response_candidate_count"] = capture_plan_data.get("source_required_response_candidate_count")
        report["mortgage_downloader_citadel_capture_plan_source_required_response_path_counts"] = capture_plan_data.get("source_required_response_path_counts")
        report["mortgage_downloader_citadel_capture_plan_safe_to_run_automatically"] = capture_plan_data.get("safe_to_run_automatically")
        report["mortgage_downloader_citadel_capture_plan_raw_source_urls_included"] = capture_plan_data.get("raw_source_urls_included")
        report["mortgage_downloader_citadel_capture_plan_secrets_included"] = capture_plan_data.get("secrets_included")
    except Exception as exc:
        report["mortgage_downloader_citadel_capture_plan_status"] = "unreadable"
        report["mortgage_downloader_citadel_capture_plan_error"] = str(exc)
preflight_report = report.get("mortgage_downloader_citadel_auth_preflight_report")
if preflight_report and os.path.exists(preflight_report):
    try:
        with open(preflight_report, encoding="utf-8") as f:
            preflight_data = json.load(f)
        report["mortgage_downloader_citadel_auth_preflight_status"] = preflight_data.get("status")
        report["mortgage_downloader_citadel_auth_preflight_reason"] = preflight_data.get("reason")
        report["mortgage_downloader_citadel_auth_preflight_manual_auth_required"] = preflight_data.get("manual_auth_required")
        report["mortgage_downloader_citadel_auth_preflight_candidate_count"] = preflight_data.get("candidate_count")
        report["mortgage_downloader_citadel_auth_preflight_route_counts"] = preflight_data.get("route_counts")
        report["mortgage_downloader_citadel_auth_preflight_login_tab_count"] = preflight_data.get("login_tab_count")
        report["mortgage_downloader_citadel_auth_preflight_non_login_tab_count"] = preflight_data.get("non_login_tab_count")
        report["mortgage_downloader_citadel_auth_preflight_scanned_count"] = preflight_data.get("scanned_count")
        report["mortgage_downloader_citadel_auth_preflight_authenticated_found"] = preflight_data.get("authenticated_found")
    except Exception as exc:
        report["mortgage_downloader_citadel_auth_preflight_status"] = "unreadable"
        report["mortgage_downloader_citadel_auth_preflight_error"] = str(exc)
downloader_report = report.get("mortgage_downloader_report")
if downloader_report and os.path.exists(downloader_report):
    try:
        with open(downloader_report, encoding="utf-8") as f:
            downloader_data = json.load(f)
        report["mortgage_downloader_status"] = downloader_data.get("status")
        report["mortgage_downloader_profile"] = downloader_data.get("profile")
        downloader_target_month = downloader_data.get("target_month") or downloader_data.get("downloader_effective_statement_target_month")
        if downloader_target_month is None:
            expected_months = downloader_data.get("downloader_expected_target_months")
            if isinstance(expected_months, list) and len(expected_months) == 1:
                downloader_target_month = expected_months[0]
        if downloader_target_month is None:
            for row in downloader_data.get("target_month_statement_gaps") or []:
                if isinstance(row, dict) and row.get("target_month"):
                    downloader_target_month = row.get("target_month")
                    break
        if downloader_target_month is None:
            for row in downloader_data.get("downloader_summaries") or []:
                if isinstance(row, dict) and row.get("target_month"):
                    downloader_target_month = row.get("target_month")
                    break
        report["mortgage_downloader_target_month"] = downloader_target_month
        downloader_statement_target_month = (
            downloader_data.get("downloader_effective_statement_target_month")
            or (
                downloader_data.get("downloader_expected_target_months")[0]
                if isinstance(downloader_data.get("downloader_expected_target_months"), list)
                and len(downloader_data.get("downloader_expected_target_months")) == 1
                else None
            )
            or downloader_target_month
        )
        report["mortgage_downloader_statement_target_month"] = downloader_statement_target_month
        report["mortgage_downloader_enabled_count"] = downloader_data.get("enabled_count")
        report["mortgage_downloader_eligible_count"] = downloader_data.get("eligible_count")
        report["mortgage_downloader_skipped_non_co_owner_paid_count"] = downloader_data.get("skipped_non_co_owner_paid_count")
        report["mortgage_downloader_success_count"] = downloader_data.get("success_count")
        report["mortgage_downloader_failed_count"] = downloader_data.get("failed_count")
        report["mortgage_downloader_skipped_count"] = downloader_data.get("skipped_count")
        report["mortgage_downloader_expected_target_months"] = downloader_data.get("downloader_expected_target_months")
        report["mortgage_downloader_expected_target_month_count"] = downloader_data.get("downloader_expected_target_month_count")
        report["mortgage_downloader_effective_statement_target_month"] = downloader_data.get("downloader_effective_statement_target_month")
        report["mortgage_downloader_target_month_matches_all_expected_months"] = downloader_data.get("target_month_matches_all_downloader_expected_months")
        report["mortgage_downloader_target_month_differs_from_expected_months"] = downloader_data.get("target_month_differs_from_downloader_expected_months")
        report["mortgage_downloader_target_month_statement_available_count"] = downloader_data.get("target_month_statement_available_count")
        report["mortgage_downloader_target_month_statement_gap_count"] = downloader_data.get("target_month_statement_gap_count")
        report["mortgage_downloader_target_month_statement_gap_properties"] = downloader_data.get("target_month_statement_gap_properties")
        report["mortgage_downloader_target_month_statement_gaps"] = downloader_data.get("target_month_statement_gaps")
        report["mortgage_downloader_current_cycle_statement_ready"] = downloader_data.get("current_cycle_statement_ready")
        if report["mortgage_downloader_current_cycle_statement_ready"] is None:
            report["mortgage_downloader_current_cycle_statement_ready"] = (
                downloader_data.get("eligible_count", 0) > 0
                and downloader_data.get("target_month_statement_gap_count", 0) == 0
                and downloader_data.get("target_month_statement_available_count", 0) == downloader_data.get("eligible_count", 0)
            )
        report["mortgage_downloader_current_cycle_statement_ready_count"] = downloader_data.get(
            "current_cycle_statement_ready_count",
            downloader_data.get("target_month_statement_available_count"),
        )
        report["mortgage_downloader_current_cycle_statement_blocker_count"] = downloader_data.get(
            "current_cycle_statement_blocker_count",
            downloader_data.get("target_month_statement_gap_count"),
        )
        report["mortgage_downloader_current_cycle_statement_blocker_properties"] = downloader_data.get(
            "current_cycle_statement_blocker_properties",
            downloader_data.get("target_month_statement_gap_properties"),
        )
        report["mortgage_downloader_current_cycle_future_automation_attention_required"] = downloader_data.get(
            "current_cycle_future_automation_attention_required",
            bool(downloader_data.get("automation_attention_count")),
        )
        report["mortgage_downloader_current_cycle_future_automation_attention_properties"] = downloader_data.get(
            "current_cycle_future_automation_attention_properties",
            downloader_data.get("automation_attention_properties"),
        )
        report["mortgage_downloader_current_cycle_future_automation_attention_reasons"] = downloader_data.get(
            "current_cycle_future_automation_attention_reasons",
            downloader_data.get("automation_attention_reasons"),
        )
        report["mortgage_downloader_failed_target_month_statement_available_count"] = downloader_data.get("failed_downloader_target_month_statement_available_count")
        report["mortgage_downloader_failed_target_month_statement_available_properties"] = downloader_data.get("failed_downloader_target_month_statement_available_properties")
        report["mortgage_downloader_failed_target_month_statement_available_details"] = downloader_data.get("failed_downloader_target_month_statement_available_details")
        report["mortgage_downloader_safe_to_run_automatically"] = downloader_data.get("safe_to_run_automatically")
        report["mortgage_downloader_safe_downloader_count"] = downloader_data.get("safe_downloader_count")
        report["mortgage_downloader_unsafe_downloader_count"] = downloader_data.get("unsafe_downloader_count")
        report["mortgage_downloader_unsafe_downloader_properties"] = downloader_data.get("unsafe_downloader_properties")
        report["mortgage_downloader_unsafe_downloader_details"] = downloader_data.get("unsafe_downloader_details")
        report["mortgage_downloader_automation_attention_count"] = downloader_data.get("automation_attention_count")
        report["mortgage_downloader_automation_attention_properties"] = downloader_data.get("automation_attention_properties")
        report["mortgage_downloader_automation_attention_reasons"] = downloader_data.get("automation_attention_reasons")
        report["mortgage_downloader_automation_attention_details"] = downloader_data.get("automation_attention_details")
        report["mortgage_downloader_results"] = downloader_data.get("results")
        report["mortgage_downloader_summaries"] = downloader_data.get("downloader_summaries")
        report["mortgage_downloader_status_counts"] = downloader_data.get("downloader_status_counts")
        report["mortgage_downloader_citadel_prepare_rc"] = downloader_data.get("citadel_prepare_rc")
        report["mortgage_downloader_citadel_prepare_status"] = downloader_data.get("citadel_prepare_status")
        report["mortgage_downloader_citadel_prepare_reason"] = downloader_data.get("citadel_prepare_reason")
        report["mortgage_downloader_citadel_prepare_successful_password_request_found"] = downloader_data.get("citadel_prepare_successful_password_request_found")
        report["mortgage_downloader_citadel_prepare_bw_session_status"] = downloader_data.get("citadel_prepare_bw_session_status")
        report["mortgage_downloader_citadel_prepare_bw_item_found"] = downloader_data.get("citadel_prepare_bw_item_found")
        report["mortgage_downloader_citadel_prepare_bw_item_name"] = downloader_data.get("citadel_prepare_bw_item_name")
        report["mortgage_downloader_citadel_prepare_bw_item_uri_host_match"] = downloader_data.get("citadel_prepare_bw_item_uri_host_match")
        report["mortgage_downloader_citadel_prepare_username_matches_har"] = downloader_data.get("citadel_prepare_username_matches_har")
        report["mortgage_downloader_citadel_prepare_password_matched_before_update"] = downloader_data.get("citadel_prepare_password_matched_before_update")
        report["mortgage_downloader_citadel_prepare_password_updated"] = downloader_data.get("citadel_prepare_password_updated")
        report["mortgage_downloader_citadel_prepare_bw_sync_attempted"] = downloader_data.get("citadel_prepare_bw_sync_attempted")
        report["mortgage_downloader_citadel_auth_retryable_portal_failure"] = downloader_data.get("citadel_auth_retryable_portal_failure")
        report["mortgage_downloader_citadel_auth_retryable_reason"] = downloader_data.get("citadel_auth_retryable_reason")
        report["mortgage_downloader_citadel_capture_report"] = downloader_data.get("citadel_capture_report")
        report["mortgage_downloader_citadel_capture_status"] = downloader_data.get("citadel_capture_status")
        report["mortgage_downloader_citadel_capture_reason"] = downloader_data.get("citadel_capture_reason")
        report["mortgage_downloader_citadel_capture_har_token_available"] = downloader_data.get("citadel_capture_har_token_available")
        report["mortgage_downloader_citadel_capture_har_token_entry_count"] = downloader_data.get("citadel_capture_har_token_entry_count")
        report["mortgage_downloader_citadel_capture_har_token_mobile_source_id_available"] = downloader_data.get("citadel_capture_har_token_mobile_source_id_available")
        report["mortgage_downloader_citadel_capture_har_token_source_endpoint_paths"] = downloader_data.get("citadel_capture_har_token_source_endpoint_paths")
        report["mortgage_downloader_citadel_capture_har_token_source_document_detail_id_count"] = downloader_data.get("citadel_capture_har_token_source_document_detail_id_count")
        report["mortgage_downloader_citadel_capture_har_token_endpoint_statuses"] = downloader_data.get("citadel_capture_har_token_endpoint_statuses")
        report["mortgage_downloader_citadel_capture_next_action_status"] = downloader_data.get("citadel_capture_next_action_status")
        report["mortgage_downloader_citadel_capture_next_action_reason"] = downloader_data.get("citadel_capture_next_action_reason")
        report["mortgage_downloader_citadel_capture_next_action_command"] = downloader_data.get("citadel_capture_next_action_command")
        report["mortgage_downloader_citadel_capture_next_action_capture_command"] = downloader_data.get("citadel_capture_next_action_capture_command")
        report["mortgage_downloader_citadel_capture_next_action_capture_required"] = downloader_data.get("citadel_capture_next_action_capture_required")
        report["mortgage_downloader_citadel_capture_next_action_target_month"] = downloader_data.get("citadel_capture_next_action_target_month")
        report["mortgage_downloader_citadel_capture_next_action_target_month_replayable_document_available"] = downloader_data.get("citadel_capture_next_action_target_month_replayable_document_available")
        report["mortgage_downloader_citadel_capture_next_action_target_month_replayable_document_payload_count"] = downloader_data.get("citadel_capture_next_action_target_month_replayable_document_payload_count")
        report["mortgage_downloader_citadel_capture_next_action_replayable_statement_months"] = downloader_data.get("citadel_capture_next_action_replayable_statement_months")
        report["mortgage_downloader_citadel_capture_next_action_statement_document_months"] = downloader_data.get("citadel_capture_next_action_statement_document_months")
        report["mortgage_downloader_citadel_capture_next_action_required_response_paths"] = downloader_data.get("citadel_capture_next_action_required_response_paths")
        report["mortgage_downloader_citadel_capture_next_action_required_response_path_counts"] = downloader_data.get("citadel_capture_next_action_required_response_path_counts")
        report["mortgage_downloader_citadel_capture_next_action_required_response_path_progress"] = downloader_data.get("citadel_capture_next_action_required_response_path_progress")
        report["mortgage_downloader_citadel_capture_next_action_response_body_requirements"] = downloader_data.get("citadel_capture_next_action_response_body_requirements")
        report["mortgage_downloader_citadel_capture_manual_auth_required"] = downloader_data.get("citadel_capture_manual_auth_required")
        report["mortgage_downloader_citadel_capture_manual_auth_file"] = downloader_data.get("citadel_capture_manual_auth_file")
        report["mortgage_downloader_citadel_capture_manual_auth_portal_url"] = downloader_data.get("citadel_capture_manual_auth_portal_url")
        report["mortgage_downloader_citadel_capture_manual_auth_target_id"] = downloader_data.get("citadel_capture_manual_auth_target_id")
        report["mortgage_downloader_citadel_capture_manual_auth_next_command"] = downloader_data.get("citadel_capture_manual_auth_next_command")
        report["mortgage_downloader_citadel_capture_manual_auth_install_verified_har_dry_run_command"] = downloader_data.get("citadel_capture_manual_auth_install_verified_har_dry_run_command")
        report["mortgage_downloader_citadel_capture_manual_auth_install_verified_har_apply_command"] = downloader_data.get("citadel_capture_manual_auth_install_verified_har_apply_command")
        report["mortgage_downloader_citadel_capture_authenticated_found"] = downloader_data.get("citadel_capture_authenticated_found")
        report["mortgage_downloader_citadel_capture_candidate_count"] = downloader_data.get("citadel_capture_candidate_count")
        report["mortgage_downloader_citadel_capture_route_counts"] = downloader_data.get("citadel_capture_route_counts")
        report["mortgage_downloader_citadel_capture_login_tab_count"] = downloader_data.get("citadel_capture_login_tab_count")
        report["mortgage_downloader_citadel_capture_non_login_tab_count"] = downloader_data.get("citadel_capture_non_login_tab_count")
        report["mortgage_downloader_citadel_capture_scanned_count"] = downloader_data.get("citadel_capture_scanned_count")
        report["mortgage_downloader_citadel_capture_captured_endpoint_count"] = downloader_data.get("citadel_capture_captured_endpoint_count")
        report["mortgage_downloader_citadel_capture_captured_response_body_count"] = downloader_data.get("citadel_capture_captured_response_body_count")
        report["mortgage_downloader_citadel_capture_source_har_path_exists"] = downloader_data.get("citadel_capture_source_har_path_exists")
        report["mortgage_downloader_citadel_capture_source_direct_pdf_limit"] = downloader_data.get("citadel_capture_source_direct_pdf_limit")
        report["mortgage_downloader_citadel_capture_source_direct_pdf_candidate_count"] = downloader_data.get("citadel_capture_source_direct_pdf_candidate_count")
        report["mortgage_downloader_citadel_capture_source_direct_pdf_path_counts"] = downloader_data.get("citadel_capture_source_direct_pdf_path_counts")
        report["mortgage_downloader_citadel_capture_source_direct_pdf_fetched_count"] = downloader_data.get("citadel_capture_source_direct_pdf_fetched_count")
        report["mortgage_downloader_citadel_capture_source_direct_pdf_replayable_count"] = downloader_data.get("citadel_capture_source_direct_pdf_replayable_count")
        report["mortgage_downloader_citadel_capture_source_required_response_candidate_count"] = downloader_data.get("citadel_capture_source_required_response_candidate_count")
        report["mortgage_downloader_citadel_capture_source_required_response_path_counts"] = downloader_data.get("citadel_capture_source_required_response_path_counts")
        report["mortgage_downloader_citadel_capture_source_required_response_fetched_count"] = downloader_data.get("citadel_capture_source_required_response_fetched_count")
        report["mortgage_downloader_citadel_capture_source_required_response_replayable_count"] = downloader_data.get("citadel_capture_source_required_response_replayable_count")
        report["mortgage_downloader_citadel_capture_required_response_paths"] = downloader_data.get("citadel_capture_required_response_paths")
        report["mortgage_downloader_citadel_capture_required_response_path_counts"] = downloader_data.get("citadel_capture_required_response_path_counts")
        report["mortgage_downloader_citadel_capture_required_response_path_progress"] = downloader_data.get("citadel_capture_required_response_path_progress")
        report["mortgage_downloader_citadel_capture_response_body_requirements"] = downloader_data.get("citadel_capture_response_body_requirements")
        report["mortgage_downloader_citadel_capture_response_body_requirement_role_counts"] = downloader_data.get("citadel_capture_response_body_requirement_role_counts")
        report["mortgage_downloader_citadel_capture_captured_response_body_requirement_counts"] = downloader_data.get("citadel_capture_captured_response_body_requirement_counts")
        report["mortgage_downloader_citadel_capture_response_body_requirement_role_capture_counts"] = downloader_data.get("citadel_capture_response_body_requirement_role_capture_counts")
        report["mortgage_downloader_citadel_capture_missing_response_body_requirements"] = downloader_data.get("citadel_capture_missing_response_body_requirements")
        report["mortgage_downloader_citadel_capture_missing_response_body_requirement_count"] = downloader_data.get("citadel_capture_missing_response_body_requirement_count")
        report["mortgage_downloader_citadel_capture_captured_required_response_paths"] = downloader_data.get("citadel_capture_captured_required_response_paths")
        report["mortgage_downloader_citadel_capture_captured_required_response_path_counts"] = downloader_data.get("citadel_capture_captured_required_response_path_counts")
        report["mortgage_downloader_citadel_capture_missing_required_response_paths"] = downloader_data.get("citadel_capture_missing_required_response_paths")
        report["mortgage_downloader_citadel_capture_missing_required_response_path_counts"] = downloader_data.get("citadel_capture_missing_required_response_path_counts")
        report["mortgage_downloader_citadel_capture_statement_candidate_count"] = downloader_data.get("citadel_capture_statement_candidate_count")
        report["mortgage_downloader_citadel_capture_replayable_document_payload_count"] = downloader_data.get("citadel_capture_replayable_document_payload_count")
        report["mortgage_downloader_citadel_capture_target_month"] = downloader_data.get("citadel_capture_target_month")
        report["mortgage_downloader_citadel_capture_target_month_replayable_document_available"] = downloader_data.get("citadel_capture_target_month_replayable_document_available")
        report["mortgage_downloader_citadel_capture_target_month_replayable_document_payload_count"] = downloader_data.get("citadel_capture_target_month_replayable_document_payload_count")
        report["mortgage_downloader_citadel_capture_replayable_statement_months"] = downloader_data.get("citadel_capture_replayable_statement_months")
        report["mortgage_downloader_citadel_capture_statement_document_months"] = downloader_data.get("citadel_capture_statement_document_months")
        report["mortgage_downloader_citadel_capture_direct_pdf_response_count"] = downloader_data.get("citadel_capture_direct_pdf_response_count")
        report["mortgage_downloader_citadel_capture_har_path"] = downloader_data.get("citadel_capture_har_path")
        report["mortgage_downloader_citadel_capture_error_count"] = downloader_data.get("citadel_capture_error_count")
        report["mortgage_downloader_citadel_report_status"] = downloader_data.get("citadel_report_status")
        report["mortgage_downloader_citadel_downloaded_count"] = downloader_data.get("citadel_downloaded_count")
        report["mortgage_downloader_citadel_skipped_count"] = downloader_data.get("citadel_skipped_count")
        report["mortgage_downloader_citadel_target_month"] = downloader_data.get("citadel_target_month") or report.get("stamp")
        report["mortgage_downloader_citadel_target_month_statement_available"] = downloader_data.get("citadel_target_month_statement_available")
        report["mortgage_downloader_citadel_target_month_existing_count"] = downloader_data.get("citadel_target_month_existing_count")
        report["mortgage_downloader_citadel_target_month_downloaded_count"] = downloader_data.get("citadel_target_month_downloaded_count")
        report["mortgage_downloader_citadel_target_month_skipped_count"] = downloader_data.get("citadel_target_month_skipped_count")
        report["mortgage_downloader_citadel_existing_target_month_files"] = downloader_data.get("citadel_existing_target_month_files")
        report["mortgage_downloader_citadel_downloaded_target_month_files"] = downloader_data.get("citadel_downloaded_target_month_files")
        report["mortgage_downloader_citadel_skipped_target_month_files"] = downloader_data.get("citadel_skipped_target_month_files")
        report["mortgage_downloader_citadel_safe_to_run_automatically"] = downloader_data.get("citadel_safe_to_run_automatically")
        report["mortgage_downloader_citadel_idempotent_replay_safe"] = downloader_data.get("citadel_idempotent_replay_safe")
        report["mortgage_downloader_citadel_copy_plan_safe_to_apply_automatically"] = downloader_data.get("citadel_copy_plan_safe_to_apply_automatically")
        report["mortgage_downloader_citadel_har_replay_ready_to_run_automatically"] = downloader_data.get("citadel_har_replay_ready_to_run_automatically")
        report["mortgage_downloader_citadel_automation_readiness_status"] = downloader_data.get("citadel_automation_readiness_status")
        report["mortgage_downloader_citadel_automation_blockers"] = downloader_data.get("citadel_automation_blockers")
        report["mortgage_downloader_citadel_idempotent_skip"] = downloader_data.get("citadel_idempotent_skip")
        report["mortgage_downloader_citadel_idempotent_skip_reason"] = downloader_data.get("citadel_idempotent_skip_reason")
        report["mortgage_downloader_citadel_error_count"] = downloader_data.get("citadel_error_count")
        report["mortgage_downloader_citadel_warning_count"] = downloader_data.get("citadel_warning_count")
        report["mortgage_downloader_citadel_auth_state"] = downloader_data.get("citadel_auth_state")
        report["mortgage_downloader_citadel_credentials_available"] = downloader_data.get("citadel_credentials_available")
        report["mortgage_downloader_citadel_login_mode"] = downloader_data.get("citadel_login_mode")
        report["mortgage_downloader_citadel_otp_required"] = downloader_data.get("citadel_otp_required")
        report["mortgage_downloader_citadel_otp_wait_ms"] = downloader_data.get("citadel_otp_wait_ms")
        report["mortgage_downloader_citadel_otp_file"] = downloader_data.get("citadel_otp_file")
        report["mortgage_downloader_citadel_otp_required_file"] = downloader_data.get("citadel_otp_required_file")
        report["mortgage_downloader_citadel_otp_next_command"] = downloader_data.get("citadel_otp_next_command")
        report["mortgage_downloader_citadel_manual_auth_required"] = downloader_data.get("citadel_manual_auth_required")
        report["mortgage_downloader_citadel_manual_auth_reason"] = downloader_data.get("citadel_manual_auth_reason")
        report["mortgage_downloader_citadel_manual_auth_file"] = downloader_data.get("citadel_manual_auth_file")
        report["mortgage_downloader_citadel_manual_auth_portal_url"] = downloader_data.get("citadel_manual_auth_portal_url")
        report["mortgage_downloader_citadel_auth_failure_reason"] = downloader_data.get("citadel_auth_failure_reason")
        report["mortgage_downloader_citadel_auth_failure_visible_reason"] = downloader_data.get("citadel_auth_failure_visible_reason")
        report["mortgage_downloader_citadel_auth_visible_error"] = downloader_data.get("citadel_auth_visible_error")
        report["mortgage_downloader_citadel_credential_state_drift_suspected"] = downloader_data.get("citadel_credential_state_drift_suspected")
        report["mortgage_downloader_citadel_login_form_last_result"] = downloader_data.get("citadel_login_form_last_result")
        report["mortgage_downloader_citadel_oauth_password_grant_failure_count"] = downloader_data.get("citadel_oauth_password_grant_failure_count")
        report["mortgage_downloader_citadel_oauth_password_grant_error_codes"] = downloader_data.get("citadel_oauth_password_grant_error_codes")
        report["mortgage_downloader_citadel_browser_storage_bearer_token_available"] = downloader_data.get("citadel_browser_storage_bearer_token_available")
        report["mortgage_downloader_citadel_browser_storage_mobile_source_id_available"] = downloader_data.get("citadel_browser_storage_mobile_source_id_available")
        report["mortgage_downloader_citadel_browser_storage_token_candidate_count"] = downloader_data.get("citadel_browser_storage_token_candidate_count")
        report["mortgage_downloader_citadel_api_header_mobile_source_id_available"] = downloader_data.get("citadel_api_header_mobile_source_id_available")
        report["mortgage_downloader_citadel_api_header_authorization_enabled"] = downloader_data.get("citadel_api_header_authorization_enabled")
        report["mortgage_downloader_citadel_direct_auth_status"] = downloader_data.get("citadel_direct_auth_status")
        report["mortgage_downloader_citadel_direct_auth_transport"] = downloader_data.get("citadel_direct_auth_transport")
        report["mortgage_downloader_citadel_direct_browser_fallback_attempted"] = downloader_data.get("citadel_direct_browser_fallback_attempted")
        report["mortgage_downloader_citadel_direct_browser_fallback_authenticated_found"] = downloader_data.get("citadel_direct_browser_fallback_authenticated_found")
        report["mortgage_downloader_citadel_direct_fresh_mfa_source_status"] = downloader_data.get("citadel_direct_fresh_mfa_source_status")
        report["mortgage_downloader_citadel_direct_fresh_recaptcha_token_available"] = downloader_data.get("citadel_direct_fresh_recaptcha_token_available")
        report["mortgage_downloader_citadel_direct_fresh_recaptcha_token_length"] = downloader_data.get("citadel_direct_fresh_recaptcha_token_length")
        report["mortgage_downloader_citadel_direct_recaptcha_eval_stage"] = downloader_data.get("citadel_direct_recaptcha_eval_stage")
        report["mortgage_downloader_citadel_direct_recaptcha_eval_error"] = downloader_data.get("citadel_direct_recaptcha_eval_error")
        report["mortgage_downloader_citadel_direct_recaptcha_token_action"] = downloader_data.get("citadel_direct_recaptcha_token_action")
        report["mortgage_downloader_citadel_direct_recaptcha_action_errors"] = downloader_data.get("citadel_direct_recaptcha_action_errors")
        report["mortgage_downloader_citadel_direct_mfa_process_id_header_available"] = downloader_data.get("citadel_direct_mfa_process_id_header_available")
        report["mortgage_downloader_citadel_direct_mfa_detail_available"] = downloader_data.get("citadel_direct_mfa_detail_available")
        report["mortgage_downloader_citadel_direct_mfa_request_uuid_available"] = downloader_data.get("citadel_direct_mfa_request_uuid_available")
        report["mortgage_downloader_citadel_direct_mfa_request_uuid_source"] = downloader_data.get("citadel_direct_mfa_request_uuid_source")
        report["mortgage_downloader_citadel_direct_no_mfa_handoff_after_password"] = downloader_data.get("citadel_direct_no_mfa_handoff_after_password")
        report["mortgage_downloader_citadel_direct_no_mfa_handoff_reason"] = downloader_data.get("citadel_direct_no_mfa_handoff_reason")
        report["mortgage_downloader_citadel_direct_otp_send_via_type"] = downloader_data.get("citadel_direct_otp_send_via_type")
        report["mortgage_downloader_citadel_direct_cdp_mfa_process_id_header_available"] = downloader_data.get("citadel_direct_cdp_mfa_process_id_header_available")
        report["mortgage_downloader_citadel_direct_oauth_cdp_event_count"] = downloader_data.get("citadel_direct_oauth_cdp_event_count")
        report["mortgage_downloader_citadel_direct_oauth_cdp_last_status"] = downloader_data.get("citadel_direct_oauth_cdp_last_status")
        report["mortgage_downloader_citadel_direct_password_token_request_shape_matches_har_success"] = downloader_data.get("citadel_direct_password_token_request_shape_matches_har_success")
        report["mortgage_downloader_citadel_direct_password_token_request_shape_matches_har_failure"] = downloader_data.get("citadel_direct_password_token_request_shape_matches_har_failure")
        report["mortgage_downloader_citadel_direct_password_token_request_shape_matched_har_statuses"] = downloader_data.get("citadel_direct_password_token_request_shape_matched_har_statuses")
        report["mortgage_downloader_citadel_direct_password_token_request_structure_matches_har_success"] = downloader_data.get("citadel_direct_password_token_request_structure_matches_har_success")
        report["mortgage_downloader_citadel_direct_password_token_request_structure_matches_har_failure"] = downloader_data.get("citadel_direct_password_token_request_structure_matches_har_failure")
        report["mortgage_downloader_citadel_direct_password_token_request_structure_matched_har_statuses"] = downloader_data.get("citadel_direct_password_token_request_structure_matched_har_statuses")
        report["mortgage_downloader_citadel_direct_error_codes"] = downloader_data.get("citadel_direct_error_codes")
        report["mortgage_downloader_citadel_direct_error_categories"] = downloader_data.get("citadel_direct_error_categories")
        report["mortgage_downloader_citadel_credential_item_name"] = downloader_data.get("citadel_credential_item_name")
        report["mortgage_downloader_citadel_credential_item_uri_hosts"] = downloader_data.get("citadel_credential_item_uri_hosts")
        report["mortgage_downloader_citadel_credential_item_portal_host_match"] = downloader_data.get("citadel_credential_item_portal_host_match")
        report["mortgage_downloader_citadel_credential_item_field_names"] = downloader_data.get("citadel_credential_item_field_names")
        report["mortgage_downloader_citadel_credential_item_notes_len"] = downloader_data.get("citadel_credential_item_notes_len")
        report["mortgage_downloader_citadel_credential_login_hint_configured"] = downloader_data.get("citadel_credential_login_hint_configured")
        report["mortgage_downloader_citadel_credential_username_matches_login_hint"] = downloader_data.get("citadel_credential_username_matches_login_hint")
        report["mortgage_downloader_citadel_credential_item_name_matches_login_hint"] = downloader_data.get("citadel_credential_item_name_matches_login_hint")
        report["mortgage_downloader_citadel_credential_field_matches_login_hint"] = downloader_data.get("citadel_credential_field_matches_login_hint")
        report["mortgage_downloader_citadel_credential_username_len"] = downloader_data.get("citadel_credential_username_len")
        report["mortgage_downloader_citadel_credential_password_len"] = downloader_data.get("citadel_credential_password_len")
        report["mortgage_downloader_citadel_credential_username_has_at"] = downloader_data.get("citadel_credential_username_has_at")
        report["mortgage_downloader_citadel_har_auth_diagnostics_enabled"] = downloader_data.get("citadel_har_auth_diagnostics_enabled")
        report["mortgage_downloader_citadel_har_auth_diagnostics_skipped_reason"] = downloader_data.get("citadel_har_auth_diagnostics_skipped_reason")
        report["mortgage_downloader_citadel_har_token_credential_match_count"] = downloader_data.get("citadel_har_token_credential_match_count")
        report["mortgage_downloader_citadel_har_successful_password_token_match"] = downloader_data.get("citadel_har_successful_password_token_match")
        report["mortgage_downloader_citadel_har_mfa_process_id_header_count"] = downloader_data.get("citadel_har_mfa_process_id_header_count")
        report["mortgage_downloader_citadel_har_token_attempt_statuses"] = downloader_data.get("citadel_har_token_attempt_statuses")
        report["mortgage_downloader_citadel_har_workflow_embedded_response_body_count"] = downloader_data.get("citadel_har_workflow_embedded_response_body_count")
        report["mortgage_downloader_citadel_har_workflow_replayable_json_response_count"] = downloader_data.get("citadel_har_workflow_replayable_json_response_count")
        report["mortgage_downloader_citadel_har_workflow_replayable_document_payload_count"] = downloader_data.get("citadel_har_workflow_replayable_document_payload_count")
        report["mortgage_downloader_citadel_har_workflow_target_month"] = downloader_data.get("citadel_har_workflow_target_month")
        report["mortgage_downloader_citadel_har_workflow_target_month_replayable_document_available"] = downloader_data.get("citadel_har_workflow_target_month_replayable_document_available")
        report["mortgage_downloader_citadel_har_workflow_target_month_replayable_document_payload_count"] = downloader_data.get("citadel_har_workflow_target_month_replayable_document_payload_count")
        report["mortgage_downloader_citadel_har_workflow_replayable_statement_months"] = downloader_data.get("citadel_har_workflow_replayable_statement_months")
        report["mortgage_downloader_citadel_har_workflow_statement_document_months"] = downloader_data.get("citadel_har_workflow_statement_document_months")
        report["mortgage_downloader_citadel_har_workflow_direct_pdf_response_count"] = downloader_data.get("citadel_har_workflow_direct_pdf_response_count")
        report["mortgage_downloader_citadel_har_workflow_source_direct_pdf_candidate_count"] = downloader_data.get("citadel_har_workflow_source_direct_pdf_candidate_count")
        report["mortgage_downloader_citadel_har_workflow_source_direct_pdf_path_counts"] = downloader_data.get("citadel_har_workflow_source_direct_pdf_path_counts")
        report["mortgage_downloader_citadel_har_workflow_source_direct_pdf_filenames"] = downloader_data.get("citadel_har_workflow_source_direct_pdf_filenames")
        report["mortgage_downloader_citadel_har_workflow_source_direct_pdf_filename_candidates"] = downloader_data.get("citadel_har_workflow_source_direct_pdf_filename_candidates")
        report["mortgage_downloader_citadel_har_workflow_target_month_direct_pdf_filenames"] = downloader_data.get("citadel_har_workflow_target_month_direct_pdf_filenames")
        report["mortgage_downloader_citadel_har_workflow_target_month_direct_pdf_filename_candidates"] = downloader_data.get("citadel_har_workflow_target_month_direct_pdf_filename_candidates")
        report["mortgage_downloader_citadel_har_workflow_target_month_direct_pdf_body_missing_candidate_count"] = downloader_data.get("citadel_har_workflow_target_month_direct_pdf_body_missing_candidate_count")
        report["mortgage_downloader_citadel_har_workflow_source_required_response_candidate_count"] = downloader_data.get("citadel_har_workflow_source_required_response_candidate_count")
        report["mortgage_downloader_citadel_har_workflow_source_required_response_path_counts"] = downloader_data.get("citadel_har_workflow_source_required_response_path_counts")
        report["mortgage_downloader_citadel_har_workflow_direct_pdf_missing_response_count"] = downloader_data.get("citadel_har_workflow_direct_pdf_missing_response_count")
        report["mortgage_downloader_citadel_har_workflow_direct_pdf_missing_response_paths"] = downloader_data.get("citadel_har_workflow_direct_pdf_missing_response_paths")
        report["mortgage_downloader_citadel_har_workflow_capture_quality_status"] = downloader_data.get("citadel_har_workflow_capture_quality_status")
        report["mortgage_downloader_citadel_har_workflow_replay_blocker"] = downloader_data.get("citadel_har_workflow_replay_blocker")
        report["mortgage_downloader_citadel_har_workflow_missing_response_body_count"] = downloader_data.get("citadel_har_workflow_missing_response_body_count")
        report["mortgage_downloader_citadel_har_workflow_missing_response_body_paths"] = downloader_data.get("citadel_har_workflow_missing_response_body_paths")
        report["mortgage_downloader_citadel_har_workflow_missing_response_body_path_counts"] = downloader_data.get("citadel_har_workflow_missing_response_body_path_counts")
        report["mortgage_downloader_citadel_har_workflow_response_body_requirements"] = downloader_data.get("citadel_har_workflow_response_body_requirements")
        report["mortgage_downloader_citadel_har_workflow_embedded_access_token_count"] = downloader_data.get("citadel_har_workflow_embedded_access_token_count")
        report["mortgage_downloader_citadel_har_workflow_can_replay_documents"] = downloader_data.get("citadel_har_workflow_can_replay_documents")
        report["mortgage_downloader_citadel_har_workflow_next_action_status"] = downloader_data.get("citadel_har_workflow_next_action_status")
        report["mortgage_downloader_citadel_har_workflow_next_action_reason"] = downloader_data.get("citadel_har_workflow_next_action_reason")
        report["mortgage_downloader_citadel_har_workflow_next_action_command"] = downloader_data.get("citadel_har_workflow_next_action_command")
        report["mortgage_downloader_citadel_har_workflow_next_action_capture_command"] = downloader_data.get("citadel_har_workflow_next_action_capture_command")
        report["mortgage_downloader_citadel_har_workflow_next_action_capture_required"] = downloader_data.get("citadel_har_workflow_next_action_capture_required")
        report["mortgage_downloader_citadel_har_workflow_next_action_required_response_paths"] = downloader_data.get("citadel_har_workflow_next_action_required_response_paths")
        report["mortgage_downloader_citadel_har_workflow_next_action_response_body_requirements"] = downloader_data.get("citadel_har_workflow_next_action_response_body_requirements")
        report["mortgage_downloader_citadel_har_workflow_next_action_source_direct_pdf_filenames"] = downloader_data.get("citadel_har_workflow_next_action_source_direct_pdf_filenames")
        report["mortgage_downloader_citadel_har_workflow_next_action_target_month_direct_pdf_filenames"] = downloader_data.get("citadel_har_workflow_next_action_target_month_direct_pdf_filenames")
        report["mortgage_downloader_citadel_har_workflow_next_action_target_month_direct_pdf_body_missing_candidate_count"] = downloader_data.get("citadel_har_workflow_next_action_target_month_direct_pdf_body_missing_candidate_count")
        report["mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_report"] = downloader_data.get("citadel_har_workflow_next_action_install_verified_capture_report")
        report["mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_dry_run_command"] = downloader_data.get("citadel_har_workflow_next_action_install_verified_capture_dry_run_command")
        report["mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_apply_command"] = downloader_data.get("citadel_har_workflow_next_action_install_verified_capture_apply_command")
        report["mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_direct_dry_run_command"] = downloader_data.get("citadel_har_workflow_next_action_install_verified_capture_direct_dry_run_command")
        report["mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_direct_apply_command"] = downloader_data.get("citadel_har_workflow_next_action_install_verified_capture_direct_apply_command")
        report["mortgage_downloader_citadel_install_verified_capture_report"] = downloader_data.get("citadel_install_verified_capture_report")
        report["mortgage_downloader_citadel_install_verified_capture_apply_command"] = downloader_data.get("citadel_install_verified_capture_apply_command")
        report["mortgage_downloader_citadel_install_verified_capture_direct_apply_command"] = downloader_data.get("citadel_install_verified_capture_direct_apply_command")
        report["mortgage_downloader_citadel_tab_scan_candidate_count"] = downloader_data.get("citadel_tab_scan_candidate_count")
        report["mortgage_downloader_citadel_tab_scan_limit"] = downloader_data.get("citadel_tab_scan_limit")
        report["mortgage_downloader_citadel_tab_scan_scanned_count"] = downloader_data.get("citadel_tab_scan_scanned_count")
        report["mortgage_downloader_citadel_tab_scan_skipped_count"] = downloader_data.get("citadel_tab_scan_skipped_count")
        report["mortgage_downloader_citadel_tab_scan_fetch_timeout_ms"] = downloader_data.get("citadel_tab_scan_fetch_timeout_ms")
        report["mortgage_downloader_citadel_tab_scan_target_id_requested"] = downloader_data.get("citadel_tab_scan_target_id_requested")
        report["mortgage_downloader_citadel_tab_scan_target_id_found"] = downloader_data.get("citadel_tab_scan_target_id_found")
        report["mortgage_downloader_citadel_tab_scan_authenticated_found"] = downloader_data.get("citadel_tab_scan_authenticated_found")
        report["mortgage_downloader_citadel_tab_scan_direct_fallback_target_selected"] = downloader_data.get("citadel_tab_scan_direct_fallback_target_selected")
        report["mortgage_downloader_citadel_tab_scan_direct_fallback_target_id_requested"] = downloader_data.get("citadel_tab_scan_direct_fallback_target_id_requested")
        report["mortgage_downloader_citadel_tab_scan_direct_fallback_target_id_found"] = downloader_data.get("citadel_tab_scan_direct_fallback_target_id_found")
        report["mortgage_downloader_citadel_oauth_network_event_count"] = downloader_data.get("citadel_oauth_network_event_count")
        report["mortgage_downloader_citadel_oauth_network_statuses"] = downloader_data.get("citadel_oauth_network_statuses")
        report["mortgage_downloader_citadel_oauth_network_error_codes"] = downloader_data.get("citadel_oauth_network_error_codes")
        report["mortgage_downloader_citadel_oauth_network_request_shape_matched_har_statuses"] = downloader_data.get("citadel_oauth_network_request_shape_matched_har_statuses")
        report["mortgage_downloader_citadel_oauth_network_request_structure_matched_har_statuses"] = downloader_data.get("citadel_oauth_network_request_structure_matched_har_statuses")
    except Exception as exc:
        report["mortgage_downloader_status"] = "unreadable"
        report["mortgage_downloader_error"] = str(exc)
def set_report_if_missing(key, value):
    if report.get(key) is None and value is not None:
        report[key] = value


for target_suffix, capture_plan_suffix in (
    ("install_verified_capture_report", "install_verified_capture_report"),
    ("install_verified_capture_dry_run_command", "install_verified_capture_dry_run_command"),
    ("install_verified_capture_apply_command", "install_verified_capture_apply_command"),
    ("install_verified_capture_direct_dry_run_command", "install_verified_capture_direct_dry_run_command"),
    ("install_verified_capture_direct_apply_command", "install_verified_capture_direct_apply_command"),
):
    set_report_if_missing(
        f"mortgage_downloader_citadel_har_workflow_next_action_{target_suffix}",
        report.get(f"mortgage_downloader_citadel_har_preflight_next_action_{target_suffix}")
        or report.get(f"mortgage_downloader_citadel_capture_plan_{capture_plan_suffix}"),
    )

set_report_if_missing(
    "mortgage_downloader_citadel_install_verified_capture_report",
    report.get("mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_report")
    or report.get("mortgage_downloader_citadel_capture_plan_install_verified_capture_report"),
)
set_report_if_missing(
    "mortgage_downloader_citadel_install_verified_capture_apply_command",
    report.get("mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_apply_command")
    or report.get("mortgage_downloader_citadel_capture_plan_install_verified_capture_apply_command"),
)
set_report_if_missing(
    "mortgage_downloader_citadel_install_verified_capture_direct_apply_command",
    report.get("mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_direct_apply_command")
    or report.get("mortgage_downloader_citadel_capture_plan_install_verified_capture_direct_apply_command"),
)


capture_manual_auth_data = read_json_file(report.get("mortgage_downloader_citadel_capture_manual_auth_fallback_file"))
using_capture_manual_auth_fallback = (
    capture_manual_auth_data.get("status") == "auth_required"
    and report.get("mortgage_downloader_citadel_capture_manual_auth_next_command") in (None, "", [], {})
)
if using_capture_manual_auth_fallback:
    report["mortgage_downloader_citadel_capture_status"] = "auth_required"
    report["mortgage_downloader_citadel_capture_reason"] = capture_manual_auth_data.get("reason")
    report["mortgage_downloader_citadel_capture_manual_auth_required"] = True
    report["mortgage_downloader_citadel_capture_manual_auth_file"] = report.get(
        "mortgage_downloader_citadel_capture_manual_auth_fallback_file"
    )
    report["mortgage_downloader_citadel_capture_manual_auth_source"] = "manual_auth_fallback_file"
    set_report_if_missing("mortgage_downloader_citadel_capture_manual_auth_portal_url", capture_manual_auth_data.get("portal_url"))
    set_report_if_missing("mortgage_downloader_citadel_capture_manual_auth_target_id", capture_manual_auth_data.get("cdp_target_id"))
    set_report_if_missing("mortgage_downloader_citadel_capture_manual_auth_next_command", capture_manual_auth_data.get("next_command"))
    set_report_if_missing(
        "mortgage_downloader_citadel_capture_manual_auth_install_verified_har_dry_run_command",
        capture_manual_auth_data.get("install_verified_har_dry_run_command"),
    )
    set_report_if_missing(
        "mortgage_downloader_citadel_capture_manual_auth_install_verified_har_apply_command",
        capture_manual_auth_data.get("install_verified_har_apply_command"),
    )
    set_report_if_missing("mortgage_downloader_citadel_capture_next_action_status", "capture_authenticated_tab")
    set_report_if_missing("mortgage_downloader_citadel_capture_next_action_reason", capture_manual_auth_data.get("reason"))
    set_report_if_missing("mortgage_downloader_citadel_capture_next_action_command", capture_manual_auth_data.get("next_command"))
    set_report_if_missing("mortgage_downloader_citadel_capture_next_action_capture_command", capture_manual_auth_data.get("capture_command"))
    set_report_if_missing("mortgage_downloader_citadel_capture_next_action_capture_required", True)
    set_report_if_missing("mortgage_downloader_citadel_capture_next_action_required_response_paths", capture_manual_auth_data.get("required_response_paths"))
    set_report_if_missing("mortgage_downloader_citadel_capture_next_action_required_response_path_counts", capture_manual_auth_data.get("required_response_path_counts"))
    set_report_if_missing("mortgage_downloader_citadel_capture_next_action_required_response_path_progress", capture_manual_auth_data.get("required_response_path_progress"))
    set_report_if_missing("mortgage_downloader_citadel_capture_next_action_response_body_requirements", capture_manual_auth_data.get("response_body_requirements"))
    set_report_if_missing("mortgage_downloader_citadel_capture_required_response_paths", capture_manual_auth_data.get("required_response_paths"))
    set_report_if_missing("mortgage_downloader_citadel_capture_required_response_path_counts", capture_manual_auth_data.get("required_response_path_counts"))
    set_report_if_missing("mortgage_downloader_citadel_capture_required_response_path_progress", capture_manual_auth_data.get("required_response_path_progress"))
    set_report_if_missing("mortgage_downloader_citadel_capture_response_body_requirements", capture_manual_auth_data.get("response_body_requirements"))
    set_report_if_missing("mortgage_downloader_citadel_capture_authenticated_found", False)
    set_report_if_missing("mortgage_downloader_citadel_capture_candidate_count", capture_manual_auth_data.get("candidate_count"))
    set_report_if_missing("mortgage_downloader_citadel_capture_route_counts", capture_manual_auth_data.get("route_counts"))
    set_report_if_missing("mortgage_downloader_citadel_capture_login_tab_count", capture_manual_auth_data.get("login_tab_count"))
    set_report_if_missing("mortgage_downloader_citadel_capture_non_login_tab_count", capture_manual_auth_data.get("non_login_tab_count"))
    set_report_if_missing("mortgage_downloader_citadel_capture_scanned_count", capture_manual_auth_data.get("scanned_count"))
    set_report_if_missing("mortgage_downloader_citadel_capture_har_path", capture_manual_auth_data.get("capture_har_path"))
    set_report_if_missing("mortgage_downloader_citadel_capture_report", capture_manual_auth_data.get("report_path"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_embedded_response_body_count", har_preflight_data.get("embedded_response_body_count"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_replayable_json_response_count", har_preflight_data.get("replayable_json_response_count"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_replayable_document_payload_count", har_preflight_data.get("replayable_document_payload_count"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_target_month", har_preflight_data.get("target_month"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_target_month_replayable_document_available", har_preflight_data.get("target_month_replayable_document_available"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_target_month_replayable_document_payload_count", har_preflight_data.get("target_month_replayable_document_payload_count"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_replayable_statement_months", har_preflight_data.get("replayable_statement_months"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_statement_document_months", har_preflight_data.get("statement_document_months"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_direct_pdf_response_count", har_preflight_data.get("direct_pdf_response_count"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_source_direct_pdf_candidate_count", har_preflight_data.get("source_direct_pdf_candidate_count"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_source_direct_pdf_path_counts", har_preflight_data.get("source_direct_pdf_path_counts"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_source_direct_pdf_filenames", har_preflight_data.get("source_direct_pdf_filenames"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_source_direct_pdf_filename_candidates", har_preflight_data.get("source_direct_pdf_filename_candidates"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_target_month_direct_pdf_filenames", har_preflight_data.get("target_month_direct_pdf_filenames"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_target_month_direct_pdf_filename_candidates", har_preflight_data.get("target_month_direct_pdf_filename_candidates"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_target_month_direct_pdf_body_missing_candidate_count", har_preflight_data.get("target_month_direct_pdf_body_missing_candidate_count"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_source_required_response_candidate_count", har_preflight_data.get("source_required_response_candidate_count"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_source_required_response_path_counts", har_preflight_data.get("source_required_response_path_counts"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_direct_pdf_missing_response_count", har_preflight_data.get("direct_pdf_missing_response_count"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_direct_pdf_missing_response_paths", har_preflight_data.get("direct_pdf_missing_response_paths"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_capture_quality_status", har_preflight_data.get("capture_quality_status"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_replay_blocker", har_preflight_data.get("replay_blocker"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_missing_response_body_count", har_preflight_data.get("missing_response_body_count"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_missing_response_body_paths", har_preflight_data.get("missing_response_body_paths"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_missing_response_body_path_counts", har_preflight_data.get("missing_response_body_path_counts"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_response_body_requirements", har_preflight_data.get("response_body_requirements"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_embedded_access_token_count", har_preflight_data.get("embedded_access_token_count"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_can_replay_documents", har_preflight_data.get("can_replay_documents"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_next_action_status", har_preflight_next_action.get("status"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_next_action_reason", har_preflight_next_action.get("reason"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_next_action_command", har_preflight_next_action.get("next_command") or har_preflight_data.get("next_command"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_next_action_capture_command", har_preflight_next_action.get("capture_command"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_next_action_capture_required", har_preflight_next_action.get("capture_required"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_next_action_target_month", har_preflight_next_action.get("target_month"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_next_action_target_month_replayable_document_available", har_preflight_next_action.get("target_month_replayable_document_available"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_next_action_target_month_replayable_document_payload_count", har_preflight_next_action.get("target_month_replayable_document_payload_count"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_next_action_replayable_statement_months", har_preflight_next_action.get("replayable_statement_months"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_next_action_statement_document_months", har_preflight_next_action.get("statement_document_months"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_next_action_required_response_paths", har_preflight_next_action.get("required_response_paths"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_next_action_response_body_requirements", har_preflight_next_action.get("response_body_requirements"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_next_action_source_direct_pdf_filenames", har_preflight_next_action.get("source_direct_pdf_filenames"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_next_action_target_month_direct_pdf_filenames", har_preflight_next_action.get("target_month_direct_pdf_filenames"))
set_report_if_missing("mortgage_downloader_citadel_har_workflow_next_action_target_month_direct_pdf_body_missing_candidate_count", har_preflight_next_action.get("target_month_direct_pdf_body_missing_candidate_count"))

def compact_tokenomics_statement_record(record):
    if not isinstance(record, dict) or not record.get("co_owner_paid_mortgage"):
        return None
    selected_directory = record.get("selected_directory")
    return {
        "property": record.get("property"),
        "statement_found": record.get("statement_found") is True,
        "statement_filename": record.get("statement_filename"),
        "statement_date": record.get("statement_date"),
        "statement_month": record.get("statement_month"),
        "statement_month_status": record.get("statement_month_status"),
        "current_month_statement_available": record.get("current_month_statement_available"),
        "statement_is_pdf": record.get("statement_is_pdf") is True,
        "selected_directory": selected_directory,
        "selected_directory_name": Path(str(selected_directory)).name if selected_directory else None,
        "principal_balance_found": record.get("principal_balance") is not None,
        "statement_reference_cell": record.get("statement_reference_cell"),
        "statement_date_cell": record.get("statement_date_cell"),
        "principal_balance_cell": record.get("principal_balance_cell"),
        "write_skipped_reason": record.get("write_skipped_reason"),
    }

def compact_coverage_record(record):
    if not isinstance(record, dict) or not record.get("property"):
        return None
    selected_directory = record.get("selected_statement_directory")
    configured_downloaders = record.get("configured_downloaders")
    if not isinstance(configured_downloaders, list):
        configured_downloaders = []
    return {
        "property": record.get("property"),
        "servicer_hint": record.get("servicer_hint"),
        "coverage_status": record.get("coverage_status"),
        "operational_status": record.get("operational_status"),
        "missing_reason": record.get("missing_reason"),
        "statement_found": record.get("statement_found") is True,
        "current_month": record.get("current_month"),
        "statement_month": record.get("statement_month"),
        "statement_month_status": record.get("statement_month_status"),
        "current_month_statement_available": record.get("current_month_statement_available"),
        "latest_statement_date": record.get("latest_statement_date"),
        "latest_statement_filename": record.get("latest_statement_filename"),
        "selected_statement_directory_name": Path(str(selected_directory)).name if selected_directory else None,
        "workflow_evidence_count": record.get("workflow_evidence_count"),
        "configured_downloader_count": record.get("configured_downloader_count"),
        "active_downloader_count": record.get("active_downloader_count"),
        "automation_ready_downloader_count": record.get("automation_ready_downloader_count"),
        "automation_blocked_downloader_count": record.get("automation_blocked_downloader_count"),
        "downloader_modes": [
            item.get("downloader_mode") for item in configured_downloaders if isinstance(item, dict)
        ],
        "uses_workflow_har_evidence": [
            item.get("uses_workflow_har_evidence") for item in configured_downloaders if isinstance(item, dict)
        ],
        "requires_live_portal_auth": [
            item.get("requires_live_portal_auth") for item in configured_downloaders if isinstance(item, dict)
        ],
        "portal_url": record.get("portal_url"),
    }

tokenomics_report = report.get("tokenomics_report")
if tokenomics_report and os.path.exists(tokenomics_report):
    try:
        with open(tokenomics_report, encoding="utf-8") as f:
            tokenomics_data = json.load(f)
        tokenomics_status = tokenomics_data.get("status")
        tokenomics_effective_status = (
            "ok"
            if (
                (tokenomics_data.get("ok") is True or tokenomics_status == "NO_REPLY")
                and tokenomics_data.get("workbook_write_gate_status") == "ok"
                and tokenomics_data.get("workbook_write_ready") is not False
            )
            else ("review" if tokenomics_status == "COOWNERSHIP_MORTGAGE_TOKENOMICS_REVIEW" else tokenomics_status)
        )
        report["tokenomics_status"] = tokenomics_status
        report["tokenomics_effective_status"] = tokenomics_effective_status
        report["tokenomics_classification"] = tokenomics_data.get("classification")
        report["tokenomics_current_month"] = tokenomics_data.get("current_month")
        report["tokenomics_workbook_path"] = tokenomics_data.get("workbook_path")
        report["tokenomics_workbook_save_attempted"] = tokenomics_data.get("workbook_save_attempted")
        report["tokenomics_workbook_partial_ready_property_write_attempted"] = tokenomics_data.get(
            "workbook_partial_ready_property_write_attempted",
            tokenomics_data.get("partial_ready_property_write_attempted"),
        )
        report["tokenomics_workbook_partial_ready_property_write_applied"] = tokenomics_data.get(
            "workbook_partial_ready_property_write_applied",
            tokenomics_data.get("partial_ready_property_write_applied"),
        )
        report["tokenomics_workbook_partial_ready_property_write_reason"] = tokenomics_data.get(
            "workbook_partial_ready_property_write_reason",
            tokenomics_data.get("partial_ready_property_write_reason"),
        )
        report["tokenomics_workbook_partial_ready_property_write_property_count"] = tokenomics_data.get(
            "workbook_partial_ready_property_write_property_count",
            tokenomics_data.get("partial_ready_property_write_property_count"),
        )
        report["tokenomics_workbook_partial_ready_property_write_properties"] = tokenomics_data.get(
            "workbook_partial_ready_property_write_properties",
            tokenomics_data.get("partial_ready_property_write_properties"),
        )
        report["tokenomics_workbook_partial_ready_property_cleared_copied_stale_property_count"] = tokenomics_data.get(
            "workbook_partial_ready_property_cleared_copied_stale_property_count",
            tokenomics_data.get("partial_ready_property_cleared_copied_stale_property_count"),
        )
        report["tokenomics_workbook_partial_ready_property_cleared_copied_stale_properties"] = tokenomics_data.get(
            "workbook_partial_ready_property_cleared_copied_stale_properties",
            tokenomics_data.get("partial_ready_property_cleared_copied_stale_properties"),
        )
        report["tokenomics_created_sheet"] = tokenomics_data.get("created_sheet")
        report["tokenomics_workbook_write_ready"] = tokenomics_data.get("workbook_write_ready")
        report["tokenomics_workbook_write_gate_status"] = tokenomics_data.get("workbook_write_gate_status")
        report["tokenomics_workbook_write_ready_property_count"] = tokenomics_data.get("workbook_write_ready_property_count")
        report["tokenomics_workbook_write_ready_properties"] = tokenomics_data.get("workbook_write_ready_properties")
        report["tokenomics_workbook_ready_property_write_plan_count"] = tokenomics_data.get("workbook_ready_property_write_plan_count")
        report["tokenomics_workbook_ready_property_write_plan_properties"] = tokenomics_data.get("workbook_ready_property_write_plan_properties")
        report["tokenomics_workbook_ready_property_write_plan"] = tokenomics_data.get("workbook_ready_property_write_plan")
        report["tokenomics_workbook_ready_property_planned_cell_write_count"] = tokenomics_data.get("workbook_ready_property_planned_cell_write_count")
        report["tokenomics_workbook_ready_property_write_blocked_by_full_gate"] = tokenomics_data.get("workbook_ready_property_write_blocked_by_full_gate")
        report["tokenomics_workbook_write_blocker_count"] = tokenomics_data.get("workbook_write_blocker_count")
        report["tokenomics_workbook_write_blocker_properties"] = tokenomics_data.get("workbook_write_blocker_properties")
        report["tokenomics_workbook_write_blocker_reasons"] = tokenomics_data.get("workbook_write_blocker_reasons")
        report["tokenomics_workbook_write_blockers"] = tokenomics_data.get("workbook_write_blockers")
        report["tokenomics_property_column_count"] = tokenomics_data.get("property_column_count")
        report["tokenomics_co_owner_paid_property_count"] = tokenomics_data.get("co_owner_paid_property_count")
        report["tokenomics_skipped_non_co_owner_paid_property_count"] = tokenomics_data.get("skipped_non_co_owner_paid_property_count")
        report["tokenomics_co_owner_paid_property_alignment_status"] = tokenomics_data.get("co_owner_paid_property_alignment_status")
        report["tokenomics_co_owner_paid_property_alignment_mismatch_count"] = tokenomics_data.get("co_owner_paid_property_alignment_mismatch_count")
        report["tokenomics_co_owner_paid_property_alignment_missing_from_coverage"] = tokenomics_data.get("co_owner_paid_property_alignment_missing_from_coverage")
        report["tokenomics_co_owner_paid_property_alignment_missing_from_tokenomics"] = tokenomics_data.get("co_owner_paid_property_alignment_missing_from_tokenomics")
        report["tokenomics_co_owner_paid_property_alignment_tokenomics_properties"] = tokenomics_data.get("co_owner_paid_property_alignment_tokenomics_properties")
        report["tokenomics_co_owner_paid_property_alignment_coverage_properties"] = tokenomics_data.get("co_owner_paid_property_alignment_coverage_properties")
        report["tokenomics_co_owner_paid_property_alignment_coverage_workbook_path"] = tokenomics_data.get("co_owner_paid_property_alignment_coverage_workbook_path")
        report["tokenomics_statement_found_count"] = tokenomics_data.get("statement_found_count")
        report["tokenomics_current_month_statement_found_count"] = tokenomics_data.get("current_month_statement_found_count")
        report["tokenomics_current_month_statement_gap_count"] = tokenomics_data.get("current_month_statement_gap_count")
        report["tokenomics_current_month_statement_gap_properties"] = tokenomics_data.get("current_month_statement_gap_properties")
        report["tokenomics_current_month_statement_gaps"] = tokenomics_data.get("current_month_statement_gaps")
        report["tokenomics_current_month_statement_gap_remediation_count"] = tokenomics_data.get("current_month_statement_gap_remediation_count")
        report["tokenomics_current_month_statement_gap_remediation_available_count"] = tokenomics_data.get("current_month_statement_gap_remediation_available_count")
        report["tokenomics_current_month_statement_gap_remediation_missing_action_count"] = tokenomics_data.get("current_month_statement_gap_remediation_missing_action_count")
        report["tokenomics_current_month_statement_gap_remediation_missing_action_properties"] = tokenomics_data.get("current_month_statement_gap_remediation_missing_action_properties")
        report["tokenomics_current_month_statement_gap_remediation_plan"] = tokenomics_data.get("current_month_statement_gap_remediation_plan")
        report["tokenomics_archive_statement_candidate_count"] = tokenomics_data.get("archive_statement_candidate_count")
        report["tokenomics_archive_statement_candidate_properties"] = tokenomics_data.get("archive_statement_candidate_properties")
        report["tokenomics_archive_current_month_statement_candidate_count"] = tokenomics_data.get("archive_current_month_statement_candidate_count")
        report["tokenomics_archive_current_month_statement_candidate_properties"] = tokenomics_data.get("archive_current_month_statement_candidate_properties")
        report["tokenomics_mortgage_downloader_coverage_report"] = tokenomics_data.get("mortgage_downloader_coverage_report")
        report["tokenomics_mortgage_downloader_coverage_status"] = tokenomics_data.get("mortgage_downloader_coverage_status")
        report["tokenomics_mortgage_downloader_coverage_report_readable"] = tokenomics_data.get("mortgage_downloader_coverage_report_readable")
        report["tokenomics_stale_statement_count"] = tokenomics_data.get("stale_statement_count")
        report["tokenomics_stale_statement_properties"] = tokenomics_data.get("stale_statement_properties")
        report["tokenomics_missing_current_month_statement_count"] = tokenomics_data.get("missing_current_month_statement_count")
        report["tokenomics_missing_current_month_statement_properties"] = tokenomics_data.get("missing_current_month_statement_properties")
        report["tokenomics_pdf_statement_count"] = tokenomics_data.get("pdf_statement_count")
        report["tokenomics_statements_found"] = tokenomics_data.get("statements_found")
        report["tokenomics_balances_written"] = tokenomics_data.get("balances_written")
        report["tokenomics_statement_reference_write_count"] = tokenomics_data.get("statement_reference_write_count")
        report["tokenomics_principal_balance_write_count"] = tokenomics_data.get("principal_balance_write_count")
        report["tokenomics_cell_write_count"] = tokenomics_data.get("cell_write_count")
        report["tokenomics_safe_to_run_automatically"] = tokenomics_data.get("safe_to_run_automatically")
        tokenomics_statement_records = tokenomics_data.get("statement_records")
        if isinstance(tokenomics_statement_records, list):
            compact_records = [
                compact
                for compact in (compact_tokenomics_statement_record(record) for record in tokenomics_statement_records)
                if compact
            ]
            report["tokenomics_statement_record_count"] = len(tokenomics_statement_records)
            report["tokenomics_co_owner_paid_statement_record_count"] = len(compact_records)
            report["tokenomics_co_owner_paid_statement_records"] = compact_records
            report["tokenomics_missing_co_owner_paid_statement_properties"] = [
                record.get("property")
                for record in compact_records
                if record.get("property") and not record.get("statement_found")
            ]
    except Exception as exc:
        report["tokenomics_status"] = "unreadable"
        report["tokenomics_error"] = str(exc)
coverage_report = report.get("mortgage_downloader_coverage_report")
if coverage_report and os.path.exists(coverage_report):
    try:
        with open(coverage_report, encoding="utf-8") as f:
            coverage_data = json.load(f)
        report["mortgage_downloader_coverage_status"] = coverage_data.get("status")
        report["mortgage_downloader_coverage_tokenomics_source"] = coverage_data.get("tokenomics_source")
        report["mortgage_downloader_coverage_tokenomics_fallback_report"] = coverage_data.get("tokenomics_fallback_report")
        report["mortgage_downloader_coverage_tokenomics_fallback_report_used"] = coverage_data.get("tokenomics_fallback_report_used")
        report["mortgage_downloader_coverage_tokenomics_fallback_error"] = coverage_data.get("tokenomics_fallback_error")
        report["mortgage_downloader_coverage_config_readable"] = coverage_data.get("config_readable")
        report["mortgage_downloader_coverage_config_error"] = coverage_data.get("config_error")
        report["mortgage_downloader_coverage_co_owner_paid_property_count"] = coverage_data.get("co_owner_paid_property_count")
        report["mortgage_downloader_coverage_covered_co_owner_paid_property_count"] = coverage_data.get("covered_co_owner_paid_property_count")
        report["mortgage_downloader_coverage_automation_ready_co_owner_paid_property_count"] = coverage_data.get("automation_ready_co_owner_paid_property_count")
        report["mortgage_downloader_coverage_configured_but_not_ready_co_owner_paid_property_count"] = coverage_data.get("configured_but_not_ready_co_owner_paid_property_count")
        report["mortgage_downloader_coverage_configured_downloader_count"] = coverage_data.get("configured_downloader_count")
        report["mortgage_downloader_coverage_configured_downloader_ids"] = coverage_data.get("configured_downloader_ids")
        report["mortgage_downloader_coverage_configured_downloader_properties"] = coverage_data.get("configured_downloader_properties")
        report["mortgage_downloader_coverage_configured_downloader_latest_report_statuses"] = coverage_data.get("configured_downloader_latest_report_statuses")
        report["mortgage_downloader_coverage_configured_downloader_latest_report_reasons"] = coverage_data.get("configured_downloader_latest_report_reasons")
        report["mortgage_downloader_coverage_configured_downloader_blocker_reasons"] = coverage_data.get("configured_downloader_blocker_reasons")
        report["mortgage_downloader_coverage_active_configured_downloader_count"] = coverage_data.get("active_configured_downloader_count")
        report["mortgage_downloader_coverage_active_configured_downloader_ids"] = coverage_data.get("active_configured_downloader_ids")
        report["mortgage_downloader_coverage_automation_ready_configured_downloader_count"] = coverage_data.get("automation_ready_configured_downloader_count")
        report["mortgage_downloader_coverage_automation_ready_configured_downloader_ids"] = coverage_data.get("automation_ready_configured_downloader_ids")
        report["mortgage_downloader_coverage_automation_blocked_configured_downloader_count"] = coverage_data.get("automation_blocked_configured_downloader_count")
        report["mortgage_downloader_coverage_automation_blocked_configured_downloader_ids"] = coverage_data.get("automation_blocked_configured_downloader_ids")
        report["mortgage_downloader_coverage_active_downloader_mode_counts"] = coverage_data.get("active_downloader_mode_counts")
        report["mortgage_downloader_coverage_automation_ready_downloader_mode_counts"] = coverage_data.get("automation_ready_downloader_mode_counts")
        report["mortgage_downloader_coverage_active_har_backed_offline_extractor_count"] = coverage_data.get("active_har_backed_offline_extractor_count")
        report["mortgage_downloader_coverage_automation_ready_har_backed_offline_extractor_count"] = coverage_data.get("automation_ready_har_backed_offline_extractor_count")
        report["mortgage_downloader_coverage_active_live_cdp_portal_downloader_count"] = coverage_data.get("active_live_cdp_portal_downloader_count")
        report["mortgage_downloader_coverage_automation_ready_live_cdp_portal_downloader_count"] = coverage_data.get("automation_ready_live_cdp_portal_downloader_count")
        report["mortgage_downloader_coverage_current_month_statement_available_count"] = coverage_data.get("current_month_statement_available_count")
        report["mortgage_downloader_coverage_current_month_statement_gap_count"] = coverage_data.get("current_month_statement_gap_count")
        report["mortgage_downloader_coverage_current_month_statement_gap_properties"] = coverage_data.get("current_month_statement_gap_properties")
        report["mortgage_downloader_coverage_current_month_statement_gaps"] = coverage_data.get("current_month_statement_gaps")
        report["mortgage_downloader_coverage_downloader_target_month_statement_gap_count"] = coverage_data.get("downloader_target_month_statement_gap_count")
        report["mortgage_downloader_coverage_downloader_target_month_statement_gap_properties"] = coverage_data.get("downloader_target_month_statement_gap_properties")
        report["mortgage_downloader_coverage_downloader_target_month_statement_gaps"] = coverage_data.get("downloader_target_month_statement_gaps")
        report["mortgage_downloader_coverage_stale_statement_count"] = coverage_data.get("stale_statement_count")
        report["mortgage_downloader_coverage_stale_statement_properties"] = coverage_data.get("stale_statement_properties")
        report["mortgage_downloader_coverage_missing_current_month_statement_count"] = coverage_data.get("missing_current_month_statement_count")
        report["mortgage_downloader_coverage_missing_current_month_statement_properties"] = coverage_data.get("missing_current_month_statement_properties")
        report["mortgage_downloader_coverage_blocked_configured_downloader_count"] = coverage_data.get("blocked_configured_downloader_count")
        blocked_configured_downloader_properties = coverage_data.get("blocked_configured_downloader_properties")
        if not isinstance(blocked_configured_downloader_properties, list):
            blocked_configured_downloader_properties = []
            seen_blocked_properties = set()
            for blocked_item in coverage_data.get("blocked_configured_downloaders") or []:
                if not isinstance(blocked_item, dict):
                    continue
                blocked_property = str(blocked_item.get("property") or "").strip()
                if blocked_property and blocked_property not in seen_blocked_properties:
                    seen_blocked_properties.add(blocked_property)
                    blocked_configured_downloader_properties.append(blocked_property)
        report["mortgage_downloader_coverage_blocked_configured_downloader_properties"] = blocked_configured_downloader_properties
        report["mortgage_downloader_coverage_blocked_configured_downloaders"] = coverage_data.get("blocked_configured_downloaders")
        automation_repair_not_statement_gap_downloaders = []
        seen_repair_not_gap = set()
        for blocked_item in coverage_data.get("blocked_configured_downloaders") or []:
            if not isinstance(blocked_item, dict):
                continue
            not_statement_gap = (
                blocked_item.get("automation_repair_priority") == "automation_repair_not_statement_gap"
                or (
                    blocked_item.get("current_month_statement_available") is True
                    and blocked_item.get("statement_coverage_satisfied") is True
                    and blocked_item.get("tokenomics_statement_gap_blocking") is False
                )
            )
            if not not_statement_gap:
                continue
            marker = json.dumps(
                {
                    "downloader_id": blocked_item.get("downloader_id"),
                    "property": blocked_item.get("property"),
                },
                sort_keys=True,
            )
            if marker in seen_repair_not_gap:
                continue
            seen_repair_not_gap.add(marker)
            automation_repair_not_statement_gap_downloaders.append(blocked_item)
        report["mortgage_downloader_coverage_automation_repair_not_statement_gap_count"] = len(
            automation_repair_not_statement_gap_downloaders
        )
        report["mortgage_downloader_coverage_automation_repair_not_statement_gap_properties"] = queue_action_values(
            automation_repair_not_statement_gap_downloaders,
            "property",
        )
        report["mortgage_downloader_coverage_automation_repair_not_statement_gap_downloaders"] = automation_repair_not_statement_gap_downloaders
        report["mortgage_downloader_coverage_missing_downloader_count"] = coverage_data.get("missing_downloader_count")
        report["mortgage_downloader_coverage_missing_downloader_properties"] = coverage_data.get("missing_downloader_properties")
        report["mortgage_downloader_coverage_workflow_evidence_capture_plan_count"] = coverage_data.get("workflow_evidence_capture_plan_count")
        report["mortgage_downloader_coverage_workflow_evidence_capture_plan"] = coverage_data.get("workflow_evidence_capture_plan")
        report["mortgage_downloader_coverage_next_action_plan_count"] = coverage_data.get("next_action_plan_count")
        report["mortgage_downloader_coverage_next_action_manual_count"] = coverage_data.get("next_action_manual_count")
        report["mortgage_downloader_coverage_next_action_safe_to_run_automatically_count"] = coverage_data.get("next_action_safe_to_run_automatically_count")
        report["mortgage_downloader_coverage_next_action_plan"] = coverage_data.get("next_action_plan")
        report["mortgage_downloader_coverage_safe_to_run_automatically"] = coverage_data.get("safe_to_run_automatically")
        coverage_records = coverage_data.get("coverage_records")
        if isinstance(coverage_records, list):
            compact_records = [
                compact
                for compact in (compact_coverage_record(record) for record in coverage_records)
                if compact
            ]
            report["mortgage_downloader_coverage_record_count"] = len(coverage_records)
            report["mortgage_downloader_coverage_records"] = compact_records
    except Exception as exc:
        report["mortgage_downloader_coverage_status"] = "unreadable"
        report["mortgage_downloader_coverage_error"] = str(exc)
evidence_advance_report = report.get("mortgage_workflow_evidence_advance_report")
if evidence_advance_report and os.path.exists(evidence_advance_report):
    try:
        with open(evidence_advance_report, encoding="utf-8") as f:
            evidence_advance_data = json.load(f)
        report["mortgage_workflow_evidence_advance_status"] = evidence_advance_data.get("status")
        report["mortgage_workflow_evidence_advance_reason"] = evidence_advance_data.get("reason")
        report["mortgage_workflow_evidence_advance_property_count"] = evidence_advance_data.get("property_count")
        report["mortgage_workflow_evidence_advance_ok_count"] = evidence_advance_data.get("ok_count")
        report["mortgage_workflow_evidence_advance_review_count"] = evidence_advance_data.get("review_count")
        report["mortgage_workflow_evidence_advance_safe_to_run_automatically"] = evidence_advance_data.get("safe_to_run_automatically")
        report["mortgage_workflow_evidence_advance_apply_evidence"] = evidence_advance_data.get("apply_evidence")
        report["mortgage_workflow_evidence_advance_write_stubs"] = evidence_advance_data.get("write_stubs")
        report["mortgage_workflow_evidence_advance_install_registry_entries"] = evidence_advance_data.get("install_registry_entries")
        report["mortgage_workflow_evidence_advance_apply_registry"] = evidence_advance_data.get("apply_registry")
        report["mortgage_workflow_evidence_advance_enable_verified_registry_entry"] = evidence_advance_data.get("enable_verified_registry_entry")
        results = evidence_advance_data.get("results")
        if isinstance(results, list):
            report["mortgage_workflow_evidence_advance_results"] = [
                {
                    "property": item.get("property"),
                    "status": item.get("status"),
                    "reason": item.get("reason"),
                    "safe_to_run_automatically": item.get("safe_to_run_automatically"),
                    "har_path": item.get("har_path"),
                    "analysis_status": item.get("analysis_status"),
                    "analysis_reason": item.get("analysis_reason"),
                    "scaffold_status": item.get("scaffold_status"),
                    "scaffold_report": item.get("scaffold_report"),
                    "registry_entry_path": item.get("registry_entry_path"),
                    "registry_install_command": item.get("registry_install_command"),
                    "generated_dry_run_command": item.get("generated_dry_run_command"),
                    "generated_dry_run_status": (item.get("generated_dry_run_report") or {}).get("status") if isinstance(item.get("generated_dry_run_report"), dict) else None,
                    "generated_dry_run_verified_target_month": (item.get("generated_dry_run_report") or {}).get("verified_target_month") if isinstance(item.get("generated_dry_run_report"), dict) else None,
                    "generated_dry_run_target_month": (item.get("generated_dry_run_report") or {}).get("target_month") if isinstance(item.get("generated_dry_run_report"), dict) else None,
                    "generated_dry_run_target_month_downloadable_count": (item.get("generated_dry_run_report") or {}).get("target_month_downloadable_count") if isinstance(item.get("generated_dry_run_report"), dict) else None,
                    "generated_dry_run_child_reason": (item.get("generated_dry_run_report") or {}).get("child_reason") if isinstance(item.get("generated_dry_run_report"), dict) else None,
                    "enable_verified_registry_entry": item.get("enable_verified_registry_entry"),
                    "enable_verified_registry_entry_status": (item.get("enable_verified_registry_entry_report") or {}).get("status") if isinstance(item.get("enable_verified_registry_entry_report"), dict) else None,
                    "enable_verified_registry_entry_reason": (item.get("enable_verified_registry_entry_report") or {}).get("reason") if isinstance(item.get("enable_verified_registry_entry_report"), dict) else None,
                    "enable_verified_registry_entry_config_written": (item.get("enable_verified_registry_entry_report") or {}).get("config_written") if isinstance(item.get("enable_verified_registry_entry_report"), dict) else None,
                    "enable_verified_registry_entry_enabled_after": (item.get("enable_verified_registry_entry_report") or {}).get("enabled_after") if isinstance(item.get("enable_verified_registry_entry_report"), dict) else None,
                }
                for item in results
                if isinstance(item, dict)
            ]
    except Exception as exc:
        report["mortgage_workflow_evidence_advance_status"] = "unreadable"
        report["mortgage_workflow_evidence_advance_error"] = str(exc)
evidence_refresh_report = report.get("mortgage_workflow_evidence_refresh_report")
if evidence_refresh_report and os.path.exists(evidence_refresh_report):
    try:
        with open(evidence_refresh_report, encoding="utf-8") as f:
            evidence_refresh_data = json.load(f)
        report["mortgage_workflow_evidence_refresh_idempotency_digest"] = evidence_refresh_data.get("idempotency_digest")
        report["mortgage_workflow_evidence_refresh_status"] = evidence_refresh_data.get("status")
        report["mortgage_workflow_evidence_refresh_reason"] = evidence_refresh_data.get("reason")
        report["mortgage_workflow_evidence_refresh_property_count"] = evidence_refresh_data.get("property_count")
        report["mortgage_workflow_evidence_refresh_analysis_ready_count"] = evidence_refresh_data.get("analysis_ready_count")
        report["mortgage_workflow_evidence_refresh_analysis_blocked_count"] = evidence_refresh_data.get("analysis_blocked_count")
        report["mortgage_workflow_evidence_refresh_scaffold_ready_count"] = evidence_refresh_data.get("scaffold_ready_count")
        report["mortgage_workflow_evidence_refresh_ensure_handoffs"] = evidence_refresh_data.get("ensure_handoffs")
        report["mortgage_workflow_evidence_refresh_handoff_pending_property_count"] = evidence_refresh_data.get("handoff_pending_property_count")
        report["mortgage_workflow_evidence_refresh_handoff_pending_properties"] = evidence_refresh_data.get("handoff_pending_properties")
        report["mortgage_workflow_evidence_refresh_handoff_file_gap_count"] = evidence_refresh_data.get("handoff_file_gap_count")
        report["mortgage_workflow_evidence_refresh_handoff_file_gap_properties"] = evidence_refresh_data.get("handoff_file_gap_properties")
        report["mortgage_workflow_evidence_refresh_har_path_missing_count"] = evidence_refresh_data.get("har_path_missing_count")
        report["mortgage_workflow_evidence_refresh_har_path_missing_properties"] = evidence_refresh_data.get("har_path_missing_properties")
        report["mortgage_workflow_evidence_refresh_har_path_present_blocked_count"] = evidence_refresh_data.get("har_path_present_blocked_count")
        report["mortgage_workflow_evidence_refresh_har_path_present_blocked_properties"] = evidence_refresh_data.get("har_path_present_blocked_properties")
        report["mortgage_workflow_evidence_refresh_post_capture_check_ready_count"] = evidence_refresh_data.get("post_capture_check_ready_count")
        report["mortgage_workflow_evidence_refresh_post_capture_check_ready_properties"] = evidence_refresh_data.get("post_capture_check_ready_properties")
        report["mortgage_workflow_evidence_refresh_capture_required_before_offline_next_step_count"] = evidence_refresh_data.get("capture_required_before_offline_next_step_count")
        report["mortgage_workflow_evidence_refresh_capture_required_before_offline_next_step_properties"] = evidence_refresh_data.get("capture_required_before_offline_next_step_properties")
        report["mortgage_workflow_evidence_refresh_offline_next_step_ready_count"] = evidence_refresh_data.get("offline_next_step_ready_count")
        report["mortgage_workflow_evidence_refresh_offline_next_step_gap_count"] = evidence_refresh_data.get("offline_next_step_gap_count")
        report["mortgage_workflow_evidence_refresh_offline_next_step_gap_properties"] = evidence_refresh_data.get("offline_next_step_gap_properties")
        report["mortgage_workflow_evidence_refresh_safe_to_run_automatically"] = evidence_refresh_data.get("safe_to_run_automatically")
        report["mortgage_workflow_evidence_refresh_next_action_plan_count"] = evidence_refresh_data.get("next_action_plan_count")
        report["mortgage_workflow_evidence_refresh_next_action_manual_count"] = evidence_refresh_data.get("next_action_manual_count")
        report["mortgage_workflow_evidence_refresh_next_action_safe_to_run_automatically_count"] = evidence_refresh_data.get("next_action_safe_to_run_automatically_count")
        for key in [
            "raw_discovered_har_candidate_property_count",
            "raw_discovered_har_candidate_properties",
            "discovered_har_candidate_property_count",
            "discovered_har_candidate_properties",
            "safe_discovered_har_candidate_property_count",
            "safe_discovered_har_candidate_properties",
            "selected_discovered_har_candidate_count",
            "selected_discovered_har_candidate_properties",
            "rejected_discovered_har_candidate_count",
            "rejected_discovered_har_candidate_paths",
        ]:
            report[f"mortgage_workflow_evidence_refresh_{key}"] = evidence_refresh_data.get(key)
        stale_scaffold_cleanup_records = []
        stale_scaffold_cleanup_paths = []
        for item in evidence_refresh_data.get("results") or []:
            if not isinstance(item, dict):
                continue
            removed = item.get("stale_scaffold_artifacts_removed")
            if not isinstance(removed, list) or not removed:
                continue
            record = {
                "property": item.get("property"),
                "har_path": item.get("har_path"),
                "analysis_status": item.get("analysis_status"),
                "analysis_reason": item.get("analysis_reason"),
                "stale_scaffold_artifacts_removed": removed,
            }
            stale_scaffold_cleanup_records.append(record)
            for path in removed:
                if path not in stale_scaffold_cleanup_paths:
                    stale_scaffold_cleanup_paths.append(path)
        report["mortgage_workflow_evidence_refresh_stale_scaffold_cleanup_count"] = len(stale_scaffold_cleanup_records)
        report["mortgage_workflow_evidence_refresh_stale_scaffold_cleanup_properties"] = [
            item.get("property") for item in stale_scaffold_cleanup_records if item.get("property")
        ]
        report["mortgage_workflow_evidence_refresh_stale_scaffold_cleanup_paths"] = stale_scaffold_cleanup_paths
        report["mortgage_workflow_evidence_refresh_stale_scaffold_cleanup_records"] = stale_scaffold_cleanup_records
        report["mortgage_workflow_evidence_refresh_next_action_plan"] = evidence_refresh_data.get("next_action_plan")
    except Exception as exc:
        report["mortgage_workflow_evidence_refresh_status"] = "unreadable"
        report["mortgage_workflow_evidence_refresh_error"] = str(exc)
capture_queue_report = report.get("mortgage_workflow_capture_queue_report")
if capture_queue_report and os.path.exists(capture_queue_report):
    try:
        with open(capture_queue_report, encoding="utf-8") as f:
            capture_queue_data = json.load(f)
        report["mortgage_workflow_capture_queue_idempotency_digest"] = capture_queue_data.get("idempotency_digest")
        report["mortgage_workflow_capture_queue_status"] = capture_queue_data.get("status")
        report["mortgage_workflow_capture_queue_reason"] = capture_queue_data.get("reason")
        report["mortgage_workflow_capture_queue_candidate_capture_handoff_count"] = capture_queue_data.get("candidate_capture_handoff_count")
        report["mortgage_workflow_capture_queue_handoff_candidate_capture_count"] = capture_queue_data.get("handoff_candidate_capture_count")
        report["mortgage_workflow_capture_queue_downloader_gap_candidate_capture_count"] = capture_queue_data.get("downloader_gap_candidate_capture_count")
        report["mortgage_workflow_capture_queue_downloader_gap_candidate_capture_properties"] = capture_queue_data.get("downloader_gap_candidate_capture_properties")
        report["mortgage_workflow_capture_queue_coverage_gap_candidate_capture_count"] = capture_queue_data.get("coverage_gap_candidate_capture_count")
        report["mortgage_workflow_capture_queue_coverage_gap_candidate_capture_properties"] = capture_queue_data.get("coverage_gap_candidate_capture_properties")
        report["mortgage_workflow_capture_queue_downloader_report"] = capture_queue_data.get("downloader_report")
        report["mortgage_workflow_capture_queue_downloader_report_exists"] = capture_queue_data.get("downloader_report_exists")
        report["mortgage_workflow_capture_queue_downloader_report_status"] = capture_queue_data.get("downloader_report_status")
        report["mortgage_workflow_capture_queue_intake"] = capture_queue_data.get("intake")
        report["mortgage_workflow_capture_queue_intake_exists"] = capture_queue_data.get("intake_exists")
        report["mortgage_workflow_capture_queue_capture_count"] = capture_queue_data.get("capture_count")
        report["mortgage_workflow_capture_queue_capture_properties"] = capture_queue_data.get("capture_properties")
        report["mortgage_workflow_capture_queue_duplicate_capture_handoff_count"] = capture_queue_data.get("duplicate_capture_handoff_count")
        report["mortgage_workflow_capture_queue_duplicate_capture_handoff_properties"] = capture_queue_data.get("duplicate_capture_handoff_properties")
        report["mortgage_workflow_capture_queue_duplicate_capture_handoff_identities"] = capture_queue_data.get("duplicate_capture_handoff_identities")
        report["mortgage_workflow_capture_queue_duplicate_capture_handoffs_suppressed"] = capture_queue_data.get("duplicate_capture_handoffs_suppressed")
        report["mortgage_workflow_capture_queue_missing_har_count"] = capture_queue_data.get("missing_har_count")
        report["mortgage_workflow_capture_queue_missing_har_properties"] = capture_queue_data.get("missing_har_properties")
        report["mortgage_workflow_capture_queue_existing_har_needs_bodies_count"] = capture_queue_data.get("existing_har_needs_bodies_count")
        report["mortgage_workflow_capture_queue_existing_har_needs_bodies_properties"] = capture_queue_data.get("existing_har_needs_bodies_properties")
        report["mortgage_workflow_capture_queue_existing_har_needs_target_month_statement_pdf_count"] = capture_queue_data.get("existing_har_needs_target_month_statement_pdf_count")
        report["mortgage_workflow_capture_queue_existing_har_needs_target_month_statement_pdf_properties"] = capture_queue_data.get("existing_har_needs_target_month_statement_pdf_properties")
        report["mortgage_workflow_capture_queue_target_month_workflow_har_capture_required_count"] = capture_queue_data.get("target_month_workflow_har_capture_required_count")
        report["mortgage_workflow_capture_queue_target_month_workflow_har_capture_required_properties"] = capture_queue_data.get("target_month_workflow_har_capture_required_properties")
        report["mortgage_workflow_capture_queue_existing_workflow_har_paths"] = capture_queue_data.get("existing_workflow_har_paths")
        report["mortgage_workflow_capture_queue_existing_workflow_har_path_sizes"] = capture_queue_data.get("existing_workflow_har_path_sizes")
        report["mortgage_workflow_capture_queue_rejected_discovered_har_candidate_count"] = capture_queue_data.get("rejected_discovered_har_candidate_count")
        report["mortgage_workflow_capture_queue_rejected_discovered_har_candidate_paths"] = capture_queue_data.get("rejected_discovered_har_candidate_paths")
        report["mortgage_workflow_capture_queue_entries"] = capture_queue_data.get("entries")
        queue_actions = queue_action_summary(capture_queue_data.get("entries"))
        report["mortgage_workflow_capture_queue_actions"] = queue_actions
        summarize_queue_command_preconditions(report, "mortgage_workflow_capture_queue", queue_actions)
        credential_plan = credential_repair_plan(queue_actions, report.get("mortgage_statement_target_month"))
        report["mortgage_workflow_credential_repair_action_count"] = len(credential_plan)
        report["mortgage_workflow_credential_repair_properties"] = queue_action_values(
            credential_plan,
            "property",
        )
        report["mortgage_workflow_credential_repair_actions"] = credential_plan
        portal_auth_plan = portal_auth_repair_plan(queue_actions, report.get("mortgage_statement_target_month"))
        report["mortgage_workflow_portal_auth_repair_action_count"] = len(portal_auth_plan)
        report["mortgage_workflow_portal_auth_repair_properties"] = queue_action_values(
            portal_auth_plan,
            "property",
        )
        report["mortgage_workflow_portal_auth_repair_actions"] = portal_auth_plan
        report["mortgage_workflow_capture_queue_har_paths"] = queue_action_values(queue_actions, "har_path")
        if not report.get("mortgage_workflow_capture_queue_existing_workflow_har_paths"):
            report["mortgage_workflow_capture_queue_existing_workflow_har_paths"] = queue_action_values(queue_actions, "existing_workflow_har_path")
        if not report.get("mortgage_workflow_capture_queue_existing_workflow_har_path_sizes"):
            report["mortgage_workflow_capture_queue_existing_workflow_har_path_sizes"] = queue_action_values(queue_actions, "existing_workflow_har_path_size")
        report["mortgage_workflow_capture_queue_capture_commands"] = queue_action_values(queue_actions, "capture_command")
        report["mortgage_workflow_capture_queue_validation_commands"] = queue_action_values(queue_actions, "validation_command")
        report["mortgage_workflow_capture_queue_advance_commands"] = queue_action_values(queue_actions, "advance_command")
        report["mortgage_workflow_capture_queue_advance_apply_commands"] = queue_action_values(queue_actions, "advance_apply_command")
        report["mortgage_workflow_capture_queue_safe_to_run_automatically"] = capture_queue_data.get("safe_to_run_automatically")
        report["mortgage_workflow_capture_queue_manual_capture_count"] = capture_queue_data.get("manual_capture_count")
        report["mortgage_workflow_capture_queue_manual_capture_properties"] = capture_queue_data.get("manual_capture_properties")
        report["mortgage_workflow_capture_queue_automatic_capture_count"] = capture_queue_data.get("automatic_capture_count")
        report["mortgage_workflow_capture_queue_automatic_capture_properties"] = capture_queue_data.get("automatic_capture_properties")
        report["mortgage_workflow_capture_queue_tokenomics_report"] = capture_queue_data.get("tokenomics_report")
        report["mortgage_workflow_capture_queue_tokenomics_report_exists"] = capture_queue_data.get("tokenomics_report_exists")
        report["mortgage_workflow_capture_queue_tokenomics_status"] = capture_queue_data.get("tokenomics_status")
        report["mortgage_workflow_capture_queue_tokenomics_current_month"] = capture_queue_data.get("tokenomics_current_month")
        report["mortgage_workflow_capture_queue_tokenomics_current_month_statement_gap_properties"] = capture_queue_data.get("tokenomics_current_month_statement_gap_properties")
        report["mortgage_workflow_capture_queue_tokenomics_blocking_count"] = capture_queue_data.get("tokenomics_blocking_count")
        report["mortgage_workflow_capture_queue_tokenomics_blocking_properties"] = capture_queue_data.get("tokenomics_blocking_properties")
        report["mortgage_workflow_capture_queue_tokenomics_non_blocking_count"] = capture_queue_data.get("tokenomics_non_blocking_count")
        report["mortgage_workflow_capture_queue_tokenomics_non_blocking_properties"] = capture_queue_data.get("tokenomics_non_blocking_properties")
        report["mortgage_workflow_capture_queue_tokenomics_workbook_write_blocker_count"] = capture_queue_data.get("tokenomics_workbook_write_blocker_count")
        report["mortgage_workflow_capture_queue_tokenomics_workbook_write_blocker_properties"] = capture_queue_data.get("tokenomics_workbook_write_blocker_properties")
        report["mortgage_workflow_capture_queue_tokenomics_workbook_write_blocker_reasons"] = capture_queue_data.get("tokenomics_workbook_write_blocker_reasons")
        merge_capture_queue_auth_context(
            report,
            "mortgage_workflow_evidence_refresh_next_action_plan",
            "mortgage_downloader_coverage_next_action_plan",
            "mortgage_workflow_replayability_next_action_plan",
        )
    except Exception as exc:
        report["mortgage_workflow_capture_queue_status"] = "unreadable"
        report["mortgage_workflow_capture_queue_error"] = str(exc)
capture_queue_processor_report = report.get("mortgage_workflow_capture_queue_processor_report")
if capture_queue_processor_report and os.path.exists(capture_queue_processor_report):
    try:
        with open(capture_queue_processor_report, encoding="utf-8") as f:
            processor_data = json.load(f)
        report["mortgage_workflow_capture_queue_processor_idempotency_digest"] = processor_data.get("idempotency_digest")
        report["mortgage_workflow_capture_queue_processor_markdown"] = processor_data.get("markdown") or report.get("mortgage_workflow_capture_queue_processor_markdown")
        report["mortgage_workflow_capture_queue_processor_status"] = processor_data.get("status")
        report["mortgage_workflow_capture_queue_processor_reason"] = processor_data.get("reason")
        report["mortgage_workflow_capture_queue_processor_apply"] = processor_data.get("apply")
        report["mortgage_workflow_capture_queue_processor_process_existing_body_recaptures"] = processor_data.get("process_existing_body_recaptures")
        report["mortgage_workflow_capture_queue_processor_enable_verified_registry_entry"] = processor_data.get("enable_verified_registry_entry")
        report["mortgage_workflow_capture_queue_processor_entry_count"] = processor_data.get("entry_count")
        report["mortgage_workflow_capture_queue_processor_processed_count"] = processor_data.get("processed_count")
        report["mortgage_workflow_capture_queue_processor_applied_count"] = processor_data.get("applied_count")
        report["mortgage_workflow_capture_queue_processor_dry_run_processed_count"] = processor_data.get("dry_run_processed_count")
        report["mortgage_workflow_capture_queue_processor_pending_apply_count"] = processor_data.get("pending_apply_count")
        report["mortgage_workflow_capture_queue_processor_pending_apply_properties"] = processor_data.get("pending_apply_properties")
        report["mortgage_workflow_capture_queue_processor_durable_completion_count"] = processor_data.get("durable_completion_count")
        report["mortgage_workflow_capture_queue_processor_durable_completion_properties"] = processor_data.get("durable_completion_properties")
        report["mortgage_workflow_capture_queue_processor_ok_count"] = processor_data.get("ok_count")
        report["mortgage_workflow_capture_queue_processor_review_count"] = processor_data.get("review_count")
        report["mortgage_workflow_capture_queue_processor_skipped_count"] = processor_data.get("skipped_count")
        report["mortgage_workflow_capture_queue_processor_missing_har_count"] = processor_data.get("missing_har_count")
        report["mortgage_workflow_capture_queue_processor_missing_har_properties"] = processor_data.get("missing_har_properties")
        report["mortgage_workflow_capture_queue_processor_invalid_har_count"] = processor_data.get("invalid_har_count")
        report["mortgage_workflow_capture_queue_processor_invalid_har_properties"] = processor_data.get("invalid_har_properties")
        report["mortgage_workflow_capture_queue_processor_auth_blocked_before_capture_count"] = processor_data.get("auth_blocked_before_capture_count")
        report["mortgage_workflow_capture_queue_processor_auth_blocked_before_capture_properties"] = processor_data.get("auth_blocked_before_capture_properties")
        report["mortgage_workflow_capture_queue_processor_auth_blocked_capture_requirements"] = processor_data.get("auth_blocked_capture_requirements")
        report["mortgage_workflow_capture_queue_processor_existing_har_recapture_skipped_count"] = processor_data.get("existing_har_recapture_skipped_count")
        report["mortgage_workflow_capture_queue_processor_existing_har_recapture_skipped_properties"] = processor_data.get("existing_har_recapture_skipped_properties")
        report["mortgage_workflow_capture_queue_processor_existing_har_recapture_requirements"] = processor_data.get("existing_har_recapture_requirements")
        report["mortgage_workflow_capture_queue_processor_existing_har_target_month_pdf_recapture_skipped_count"] = processor_data.get("existing_har_target_month_pdf_recapture_skipped_count")
        report["mortgage_workflow_capture_queue_processor_existing_har_target_month_pdf_recapture_skipped_properties"] = processor_data.get("existing_har_target_month_pdf_recapture_skipped_properties")
        report["mortgage_workflow_capture_queue_processor_existing_har_target_month_pdf_recapture_requirements"] = processor_data.get("existing_har_target_month_pdf_recapture_requirements")
        report["mortgage_workflow_capture_queue_processor_existing_body_recapture_skipped_count"] = processor_data.get("existing_body_recapture_skipped_count")
        report["mortgage_workflow_capture_queue_processor_existing_body_recapture_skipped_properties"] = processor_data.get("existing_body_recapture_skipped_properties")
        report["mortgage_workflow_capture_queue_processor_existing_body_recapture_requirements"] = processor_data.get("existing_body_recapture_requirements")
        report["mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocker_count"] = processor_data.get("tokenomics_workbook_write_blocker_count")
        report["mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocker_properties"] = processor_data.get("tokenomics_workbook_write_blocker_properties")
        report["mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocker_reasons"] = processor_data.get("tokenomics_workbook_write_blocker_reasons")
        report["mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocker_statement_month_statuses"] = processor_data.get("tokenomics_workbook_write_blocker_statement_month_statuses")
        report["mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocking_count"] = (
            processor_data.get("tokenomics_workbook_write_blocking_count")
            if processor_data.get("tokenomics_workbook_write_blocking_count") is not None
            else processor_data.get("tokenomics_workbook_write_blocker_count")
        )
        report["mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocking_properties"] = (
            processor_data.get("tokenomics_workbook_write_blocking_properties")
            if processor_data.get("tokenomics_workbook_write_blocking_properties") is not None
            else processor_data.get("tokenomics_workbook_write_blocker_properties")
        )
        report["mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocking_reasons"] = (
            processor_data.get("tokenomics_workbook_write_blocking_reasons")
            if processor_data.get("tokenomics_workbook_write_blocking_reasons") is not None
            else processor_data.get("tokenomics_workbook_write_blocker_reasons")
        )
        report["mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocking_statement_month_statuses"] = (
            processor_data.get("tokenomics_workbook_write_blocking_statement_month_statuses")
            if processor_data.get("tokenomics_workbook_write_blocking_statement_month_statuses") is not None
            else processor_data.get("tokenomics_workbook_write_blocker_statement_month_statuses")
        )
        report["tokenomics_workbook_write_blocking_property_count"] = report[
            "mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocking_count"
        ]
        report["tokenomics_workbook_write_blocking_properties"] = report[
            "mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocking_properties"
        ]
        report["tokenomics_workbook_write_blocking_reasons"] = report[
            "mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocking_reasons"
        ]
        report["tokenomics_workbook_write_blocking_statement_month_statuses"] = report[
            "mortgage_workflow_capture_queue_processor_tokenomics_workbook_write_blocking_statement_month_statuses"
        ]
        report["coownership_tokenomics_workbook_write_blocking_property_count"] = report[
            "tokenomics_workbook_write_blocking_property_count"
        ]
        report["coownership_tokenomics_workbook_write_blocking_properties"] = report[
            "tokenomics_workbook_write_blocking_properties"
        ]
        report["mortgage_workflow_capture_queue_processor_tokenomics_queue_blocker_count"] = processor_data.get("tokenomics_queue_blocker_count")
        report["mortgage_workflow_capture_queue_processor_tokenomics_queue_blocker_properties"] = processor_data.get("tokenomics_queue_blocker_properties")
        report["mortgage_workflow_capture_queue_processor_queue_blocker_status"] = processor_data.get("queue_blocker_status")
        report["mortgage_workflow_capture_queue_processor_queue_blocker_reason"] = processor_data.get("queue_blocker_reason")
        report["mortgage_workflow_capture_queue_processor_queue_blocker_count"] = processor_data.get("queue_blocker_count")
        report["mortgage_workflow_capture_queue_processor_queue_blocker_properties"] = processor_data.get("queue_blocker_properties")
        report["mortgage_workflow_capture_queue_processor_operator_next_action_count"] = processor_data.get("operator_next_action_count")
        report["mortgage_workflow_capture_queue_processor_operator_next_action_properties"] = processor_data.get("operator_next_action_properties")
        report["mortgage_workflow_capture_queue_processor_operator_next_actions"] = processor_data.get("operator_next_actions")
        report["mortgage_workflow_capture_queue_processor_safe_to_run_automatically"] = processor_data.get("safe_to_run_automatically")
        report["mortgage_workflow_capture_queue_processor_results"] = processor_data.get("results")
        summarize_queue_command_preconditions(
            report,
            "mortgage_workflow_capture_queue_processor",
            report.get("mortgage_workflow_capture_queue_processor_results"),
        )
        processor_credential_plan = credential_repair_plan(
            report.get("mortgage_workflow_capture_queue_processor_results"),
            report.get("mortgage_statement_target_month"),
        )
        report["mortgage_workflow_capture_queue_processor_credential_repair_action_count"] = len(
            processor_credential_plan
        )
        report["mortgage_workflow_capture_queue_processor_credential_repair_properties"] = queue_action_values(
            processor_credential_plan,
            "property",
        )
        report["mortgage_workflow_capture_queue_processor_credential_repair_actions"] = processor_credential_plan
        processor_portal_auth_plan = portal_auth_repair_plan(
            report.get("mortgage_workflow_capture_queue_processor_results"),
            report.get("mortgage_statement_target_month"),
        )
        report["mortgage_workflow_capture_queue_processor_portal_auth_repair_action_count"] = len(
            processor_portal_auth_plan
        )
        report["mortgage_workflow_capture_queue_processor_portal_auth_repair_properties"] = queue_action_values(
            processor_portal_auth_plan,
            "property",
        )
        report["mortgage_workflow_capture_queue_processor_portal_auth_repair_actions"] = processor_portal_auth_plan
        live_cdp_handoffs = live_cdp_downloader_handoffs(
            report.get("mortgage_workflow_capture_queue_processor_operator_next_actions"),
            report.get("mortgage_workflow_capture_queue_processor_existing_har_recapture_requirements"),
            report.get("mortgage_workflow_capture_queue_processor_existing_har_target_month_pdf_recapture_requirements"),
            report.get("mortgage_workflow_capture_queue_processor_existing_body_recapture_requirements"),
            report.get("mortgage_workflow_capture_queue_processor_results"),
        )
        report["mortgage_workflow_capture_queue_processor_live_cdp_downloader_handoff_count"] = len(live_cdp_handoffs)
        report["mortgage_workflow_capture_queue_processor_live_cdp_downloader_handoffs"] = live_cdp_handoffs
        report["mortgage_workflow_capture_queue_processor_live_cdp_downloader_properties"] = queue_action_values(
            live_cdp_handoffs, "property"
        )
        report["mortgage_workflow_capture_queue_processor_live_cdp_downloader_commands"] = queue_action_values(
            live_cdp_handoffs, "live_cdp_downloader_command"
        )
    except Exception as exc:
        report["mortgage_workflow_capture_queue_processor_status"] = "unreadable"
        report["mortgage_workflow_capture_queue_processor_error"] = str(exc)
replayability_report = report.get("mortgage_workflow_replayability_report")
if replayability_report and os.path.exists(replayability_report):
    try:
        with open(replayability_report, encoding="utf-8") as f:
            replayability_data = json.load(f)
        report["mortgage_workflow_replayability_status"] = replayability_data.get("status")
        report["mortgage_workflow_replayability_reason"] = replayability_data.get("reason")
        report["mortgage_workflow_replayability_safe_to_run_automatically"] = replayability_data.get("safe_to_run_automatically")
        report["mortgage_workflow_replayability_har_analysis_count"] = replayability_data.get("har_analysis_count")
        report["mortgage_workflow_replayability_har_analysis_ready_count"] = replayability_data.get("har_analysis_ready_count")
        report["mortgage_workflow_replayability_har_analysis_blocked_count"] = replayability_data.get("har_analysis_blocked_count")
        report["mortgage_workflow_replayability_next_action_plan_count"] = replayability_data.get("next_action_plan_count")
        report["mortgage_workflow_replayability_next_action_manual_count"] = replayability_data.get("next_action_manual_count")
        report["mortgage_workflow_replayability_next_action_safe_to_run_automatically_count"] = replayability_data.get("next_action_safe_to_run_automatically_count")
        for key in [
            "raw_discovered_har_candidate_property_count",
            "raw_discovered_har_candidate_properties",
            "discovered_har_candidate_property_count",
            "discovered_har_candidate_properties",
            "safe_discovered_har_candidate_property_count",
            "safe_discovered_har_candidate_properties",
            "selected_discovered_har_candidate_count",
            "selected_discovered_har_candidate_properties",
            "rejected_discovered_har_candidate_count",
            "rejected_discovered_har_candidate_paths",
        ]:
            report[f"mortgage_workflow_replayability_{key}"] = replayability_data.get(key)
        report["mortgage_workflow_replayability_next_action_plan"] = replayability_data.get("next_action_plan")
        citadel_replayability = replayability_data.get("citadel") if isinstance(replayability_data.get("citadel"), dict) else {}
        report["mortgage_workflow_replayability_citadel_status"] = (
            replayability_data.get("citadel_status") or citadel_replayability.get("status")
        )
        report["mortgage_workflow_replayability_citadel_ready"] = citadel_replayability.get("ready")
        report["mortgage_workflow_replayability_citadel_blocker"] = citadel_replayability.get("blocker")
        report["mortgage_workflow_replayability_citadel_target_month"] = citadel_replayability.get("target_month")
        report["mortgage_workflow_replayability_citadel_target_month_replayable_document_available"] = citadel_replayability.get("target_month_replayable_document_available")
        report["mortgage_workflow_replayability_citadel_target_month_replayable_document_payload_count"] = citadel_replayability.get("target_month_replayable_document_payload_count")
        report["mortgage_workflow_replayability_citadel_replayable_statement_months"] = citadel_replayability.get("replayable_statement_months")
        report["mortgage_workflow_replayability_citadel_statement_document_months"] = citadel_replayability.get("statement_document_months")
        report["mortgage_workflow_replayability_citadel_direct_pdf_response_count"] = citadel_replayability.get("direct_pdf_response_count")
        report["mortgage_workflow_replayability_citadel_direct_pdf_missing_response_count"] = citadel_replayability.get("direct_pdf_missing_response_count")
        report["mortgage_workflow_replayability_citadel_direct_pdf_missing_response_paths"] = citadel_replayability.get("direct_pdf_missing_response_paths")
        report["mortgage_workflow_replayability_citadel_missing_response_body_count"] = citadel_replayability.get("missing_response_body_count")
        report["mortgage_workflow_replayability_citadel_missing_response_body_path_counts"] = citadel_replayability.get("missing_response_body_path_counts")
        merge_capture_queue_auth_context(
            report,
            "mortgage_workflow_evidence_refresh_next_action_plan",
            "mortgage_downloader_coverage_next_action_plan",
            "mortgage_workflow_replayability_next_action_plan",
            "mortgage_workflow_capture_queue_processor_results",
            "mortgage_workflow_capture_queue_processor_operator_next_actions",
            "mortgage_workflow_capture_queue_processor_existing_har_recapture_requirements",
            "mortgage_workflow_capture_queue_processor_existing_har_target_month_pdf_recapture_requirements",
            "mortgage_workflow_capture_queue_processor_existing_body_recapture_requirements",
        )
        summarize_queue_command_preconditions(
            report,
            "mortgage_workflow_replayability",
            report.get("mortgage_workflow_replayability_next_action_plan"),
        )
        replayability_credential_plan = credential_repair_plan(
            report.get("mortgage_workflow_replayability_next_action_plan"),
            report.get("mortgage_statement_target_month"),
        )
        report["mortgage_workflow_replayability_credential_repair_action_count"] = len(
            replayability_credential_plan
        )
        report["mortgage_workflow_replayability_credential_repair_properties"] = queue_action_values(
            replayability_credential_plan,
            "property",
        )
        report["mortgage_workflow_replayability_credential_repair_actions"] = replayability_credential_plan
        replayability_portal_auth_plan = portal_auth_repair_plan(
            report.get("mortgage_workflow_replayability_next_action_plan"),
            report.get("mortgage_statement_target_month"),
        )
        report["mortgage_workflow_replayability_portal_auth_repair_action_count"] = len(
            replayability_portal_auth_plan
        )
        report["mortgage_workflow_replayability_portal_auth_repair_properties"] = queue_action_values(
            replayability_portal_auth_plan,
            "property",
        )
        report["mortgage_workflow_replayability_portal_auth_repair_actions"] = replayability_portal_auth_plan
        summarize_queue_command_preconditions(
            report,
            "mortgage_workflow_capture_queue_processor",
            report.get("mortgage_workflow_capture_queue_processor_results"),
        )
        live_cdp_handoffs = live_cdp_downloader_handoffs(
            report.get("mortgage_workflow_capture_queue_processor_operator_next_actions"),
            report.get("mortgage_workflow_capture_queue_processor_existing_har_recapture_requirements"),
            report.get("mortgage_workflow_capture_queue_processor_existing_har_target_month_pdf_recapture_requirements"),
            report.get("mortgage_workflow_capture_queue_processor_existing_body_recapture_requirements"),
            report.get("mortgage_workflow_capture_queue_processor_results"),
        )
        report["mortgage_workflow_capture_queue_processor_live_cdp_downloader_handoff_count"] = len(live_cdp_handoffs)
        report["mortgage_workflow_capture_queue_processor_live_cdp_downloader_handoffs"] = live_cdp_handoffs
        report["mortgage_workflow_capture_queue_processor_live_cdp_downloader_properties"] = queue_action_values(
            live_cdp_handoffs, "property"
        )
        report["mortgage_workflow_capture_queue_processor_live_cdp_downloader_commands"] = queue_action_values(
            live_cdp_handoffs, "live_cdp_downloader_command"
        )
    except Exception as exc:
        report["mortgage_workflow_replayability_status"] = "unreadable"
        report["mortgage_workflow_replayability_error"] = str(exc)

def compact_registry_handoff_item(source, item):
    if not isinstance(item, dict) or not item.get("registry_entry_path"):
        return None
    return {
        "source": source,
        "type": item.get("type"),
        "property": item.get("property"),
        "registry_entry_path": item.get("registry_entry_path"),
        "registry_entry_name": Path(str(item.get("registry_entry_path"))).name,
        "registry_entry_written": item.get("registry_entry_written"),
        "registry_install_command": item.get("registry_install_command"),
        "safe_to_register_automatically": item.get("safe_to_register_automatically"),
        "scaffold_report": item.get("scaffold_report"),
        "manifest_path": item.get("manifest_path"),
    }

registry_handoffs = []
registry_handoff_seen = set()
for source_key, source_name in [
    ("mortgage_workflow_evidence_refresh_next_action_plan", "evidence_refresh"),
    ("mortgage_workflow_replayability_next_action_plan", "replayability"),
]:
    source_plan = report.get(source_key)
    if not isinstance(source_plan, list):
        continue
    for source_item in source_plan:
        compact_handoff = compact_registry_handoff_item(source_name, source_item)
        if not compact_handoff:
            continue
        dedupe_key = (
            compact_handoff.get("source"),
            compact_handoff.get("property"),
            compact_handoff.get("registry_entry_path"),
        )
        if dedupe_key in registry_handoff_seen:
            continue
        registry_handoff_seen.add(dedupe_key)
        registry_handoffs.append(compact_handoff)
report["mortgage_workflow_registry_handoff_count"] = len(registry_handoffs)
report["mortgage_workflow_registry_handoffs"] = registry_handoffs
report["mortgage_workflow_registry_handoff_paths"] = [
    item.get("registry_entry_path")
    for item in registry_handoffs
    if item.get("registry_entry_path")
]
report["mortgage_workflow_registry_install_commands"] = [
    item.get("registry_install_command")
    for item in registry_handoffs
    if item.get("registry_install_command")
]
report["mortgage_workflow_registry_safe_to_register_automatically_count"] = sum(
    1 for item in registry_handoffs if item.get("safe_to_register_automatically") is True
)

def normalized_property(value):
    return " ".join(str(value or "").strip().casefold().split())

def append_unique(target, value):
    if value in (None, "", []):
        return
    if isinstance(value, list):
        for subvalue in value:
            append_unique(target, subvalue)
        return
    if value not in target:
        target.append(value)

def ensure_property_matrix_row(rows, order, property_name):
    key = normalized_property(property_name)
    if not key:
        return None
    if key not in rows:
        rows[key] = {
            "property": property_name,
            "servicer_hint": None,
            "coverage_status": None,
            "operational_status": None,
            "missing_reason": None,
            "statement_found": None,
            "current_month_statement_available": None,
            "statement_month": None,
            "statement_month_status": None,
            "coverage_statement_found": None,
            "tokenomics_statement_found": None,
            "tokenomics_statement_filename": None,
            "tokenomics_statement_date": None,
            "latest_statement_filename": None,
            "latest_statement_date": None,
            "selected_directory_name": None,
            "workflow_evidence_count": None,
            "configured_downloader_count": None,
            "active_downloader_count": None,
            "automation_ready_downloader_count": None,
            "automation_blocked_downloader_count": None,
            "action_sources": [],
            "next_action_types": [],
            "suggested_next_actions": [],
            "next_action_statuses": [],
            "next_action_reasons": [],
            "next_commands": [],
            "capture_commands": [],
            "handoff_commands": [],
            "post_capture_check_commands": [],
            "registry_entry_paths": [],
            "required_response_paths": [],
            "manual_action_count": 0,
            "safe_action_count": 0,
        }
        order.append(key)
    return rows[key]

def compact_property_action(source, item):
    if not isinstance(item, dict) or not item.get("property"):
        return None
    return {
        "source": source,
        "type": item.get("type"),
        "suggested_next_action": item.get("suggested_next_action"),
        "status": item.get("next_action_status") or item.get("status"),
        "reason": item.get("reason"),
        "next_command": item.get("next_command"),
        "capture_command": item.get("capture_command"),
        "handoff_command": item.get("handoff_command"),
        "post_capture_check_command": item.get("post_capture_check_command"),
        "registry_entry_path": item.get("registry_entry_path"),
        "required_response_paths": item.get("required_response_paths") or item.get("missing_response_body_paths"),
        "safe_to_run_automatically": item.get("safe_to_run_automatically") is True,
        "safe_to_register_automatically": item.get("safe_to_register_automatically") is True,
    }

def property_matrix_status(row):
    if row.get("manual_action_count"):
        return "review"
    if row.get("current_month_statement_available") is False:
        return "current_month_statement_missing"
    if row.get("coverage_status") == "missing_downloader":
        return "needs_downloader"
    if row.get("operational_status") == "blocked":
        return "blocked_downloader"
    if row.get("statement_found") is False:
        return "statement_missing"
    if row.get("coverage_status") == "covered" and row.get("statement_found") is True:
        return "ready"
    return "review"

property_matrix_rows = {}
property_matrix_order = []
for item in report.get("mortgage_downloader_coverage_records") or []:
    row = ensure_property_matrix_row(property_matrix_rows, property_matrix_order, item.get("property"))
    if not row:
        continue
    row["servicer_hint"] = item.get("servicer_hint")
    row["coverage_status"] = item.get("coverage_status")
    row["operational_status"] = item.get("operational_status")
    row["missing_reason"] = item.get("missing_reason")
    row["coverage_statement_found"] = item.get("statement_found")
    row["current_month_statement_available"] = item.get("current_month_statement_available")
    row["statement_month"] = item.get("statement_month")
    row["statement_month_status"] = item.get("statement_month_status")
    row["latest_statement_date"] = item.get("latest_statement_date")
    row["latest_statement_filename"] = item.get("latest_statement_filename")
    row["selected_directory_name"] = item.get("selected_statement_directory_name")
    row["workflow_evidence_count"] = item.get("workflow_evidence_count")
    row["configured_downloader_count"] = item.get("configured_downloader_count")
    row["active_downloader_count"] = item.get("active_downloader_count")
    row["automation_ready_downloader_count"] = item.get("automation_ready_downloader_count")
    row["automation_blocked_downloader_count"] = item.get("automation_blocked_downloader_count")
for item in report.get("tokenomics_co_owner_paid_statement_records") or []:
    row = ensure_property_matrix_row(property_matrix_rows, property_matrix_order, item.get("property"))
    if not row:
        continue
    row["tokenomics_statement_found"] = item.get("statement_found")
    row["tokenomics_statement_filename"] = item.get("statement_filename")
    row["tokenomics_statement_date"] = item.get("statement_date")
    if item.get("selected_directory_name") and not row.get("selected_directory_name"):
        row["selected_directory_name"] = item.get("selected_directory_name")
    if item.get("statement_found") is True:
        row["latest_statement_filename"] = row.get("latest_statement_filename") or item.get("statement_filename")
        row["latest_statement_date"] = row.get("latest_statement_date") or item.get("statement_date")
for source_key, source_name in [
    ("mortgage_downloader_coverage_next_action_plan", "coverage"),
    ("mortgage_workflow_evidence_refresh_next_action_plan", "evidence_refresh"),
    ("mortgage_workflow_replayability_next_action_plan", "replayability"),
]:
    source_plan = report.get(source_key)
    if not isinstance(source_plan, list):
        continue
    for source_item in source_plan:
        action = compact_property_action(source_name, source_item)
        if not action:
            continue
        row = ensure_property_matrix_row(property_matrix_rows, property_matrix_order, source_item.get("property"))
        if not row:
            continue
        append_unique(row["action_sources"], action.get("source"))
        append_unique(row["next_action_types"], action.get("type"))
        append_unique(row["suggested_next_actions"], action.get("suggested_next_action"))
        append_unique(row["next_action_statuses"], action.get("status"))
        append_unique(row["next_action_reasons"], action.get("reason"))
        append_unique(row["next_commands"], action.get("next_command"))
        append_unique(row["capture_commands"], action.get("capture_command"))
        append_unique(row["handoff_commands"], action.get("handoff_command"))
        append_unique(row["post_capture_check_commands"], action.get("post_capture_check_command"))
        append_unique(row["registry_entry_paths"], action.get("registry_entry_path"))
        append_unique(row["required_response_paths"], action.get("required_response_paths"))
        if action.get("safe_to_run_automatically") or action.get("safe_to_register_automatically"):
            row["safe_action_count"] += 1
        else:
            row["manual_action_count"] += 1
property_matrix = []
for key in property_matrix_order:
    row = property_matrix_rows[key]
    if row.get("tokenomics_statement_found") is not None:
        row["statement_found"] = row.get("tokenomics_statement_found")
    else:
        row["statement_found"] = row.get("coverage_statement_found")
    row["workflow_status"] = property_matrix_status(row)
    property_matrix.append(row)

def workbook_blocker_reasons(row):
    reasons = []
    current_statement_available = row.get("current_month_statement_available") is True
    if row.get("current_month_statement_available") is False:
        append_unique(reasons, "current_month_statement_missing")
    if row.get("statement_month_status") == "stale":
        append_unique(reasons, "current_month_statement_stale")
    if row.get("statement_month_status") == "missing":
        append_unique(reasons, "statement_month_missing")
    if row.get("statement_found") is False:
        append_unique(reasons, "statement_missing")
    if current_statement_available:
        return reasons
    if row.get("coverage_status") == "missing_downloader":
        append_unique(reasons, "downloader_missing")
    if row.get("operational_status") == "blocked" or row.get("automation_blocked_downloader_count"):
        append_unique(reasons, "downloader_blocked")
    if row.get("workflow_evidence_count") == 0:
        append_unique(reasons, "workflow_evidence_missing")
    if row.get("manual_action_count"):
        append_unique(reasons, "manual_action_required")
    for status in row.get("next_action_statuses") or []:
        if status == "capture_full_response_bodies":
            append_unique(reasons, "missing_response_bodies")
        elif status:
            append_unique(reasons, status)
    for reason in row.get("next_action_reasons") or []:
        append_unique(reasons, reason)
    if row.get("workflow_status") not in (None, "ready"):
        append_unique(reasons, row.get("workflow_status"))
    return reasons

def compact_workbook_blocker(row, reasons):
    return {
        "property": row.get("property"),
        "reasons": reasons,
        "workflow_status": row.get("workflow_status"),
        "coverage_status": row.get("coverage_status"),
        "operational_status": row.get("operational_status"),
        "statement_found": row.get("statement_found"),
        "current_month_statement_available": row.get("current_month_statement_available"),
        "statement_month": row.get("statement_month"),
        "statement_month_status": row.get("statement_month_status"),
        "workflow_evidence_count": row.get("workflow_evidence_count"),
        "configured_downloader_count": row.get("configured_downloader_count"),
        "active_downloader_count": row.get("active_downloader_count"),
        "automation_ready_downloader_count": row.get("automation_ready_downloader_count"),
        "automation_blocked_downloader_count": row.get("automation_blocked_downloader_count"),
        "manual_action_count": row.get("manual_action_count"),
        "safe_action_count": row.get("safe_action_count"),
        "next_action_types": row.get("next_action_types"),
        "next_action_statuses": row.get("next_action_statuses"),
        "next_action_reasons": row.get("next_action_reasons"),
        "required_response_paths": row.get("required_response_paths"),
        "capture_commands": row.get("capture_commands"),
        "post_capture_check_commands": row.get("post_capture_check_commands"),
        "registry_entry_paths": row.get("registry_entry_paths"),
    }

def first_list_item(values):
    if isinstance(values, list) and values:
        return values[0]
    return None

def tokenomics_blocking_capture_item(blocker):
    if not isinstance(blocker, dict):
        return None
    if blocker.get("current_month_statement_available") is True:
        return None
    capture_commands = blocker.get("capture_commands")
    post_capture_check_commands = blocker.get("post_capture_check_commands")
    if not isinstance(capture_commands, list):
        capture_commands = []
    if not isinstance(post_capture_check_commands, list):
        post_capture_check_commands = []
    if not capture_commands and not post_capture_check_commands:
        return None
    return {
        "property": blocker.get("property"),
        "reasons": blocker.get("reasons"),
        "statement_month": blocker.get("statement_month"),
        "statement_month_status": blocker.get("statement_month_status"),
        "current_month_statement_available": blocker.get("current_month_statement_available"),
        "workflow_status": blocker.get("workflow_status"),
        "coverage_status": blocker.get("coverage_status"),
        "operational_status": blocker.get("operational_status"),
        "capture_command": first_list_item(capture_commands),
        "capture_commands": capture_commands,
        "post_capture_check_command": first_list_item(post_capture_check_commands),
        "post_capture_check_commands": post_capture_check_commands,
        "required_response_paths": blocker.get("required_response_paths"),
        "manual_action_count": blocker.get("manual_action_count"),
        "workflow_evidence_count": blocker.get("workflow_evidence_count"),
        "tokenomics_blocking": True,
        "tokenomics_statement_gap_blocking": True,
        "workbook_write_blocking": True,
        "tokenomics_workbook_write_blocking": True,
    }

workbook_write_blockers = []
workbook_write_ready_properties = []
workbook_write_blocker_reasons = []
for row in property_matrix:
    reasons = workbook_blocker_reasons(row)
    if reasons:
        workbook_write_blockers.append(compact_workbook_blocker(row, reasons))
        append_unique(workbook_write_blocker_reasons, reasons)
    elif row.get("property"):
        workbook_write_ready_properties.append(row.get("property"))
tokenomics_blocking_capture_queue = [
    item
    for item in (tokenomics_blocking_capture_item(blocker) for blocker in workbook_write_blockers)
    if item
]
automation_blocked_with_current_statement = [
    row
    for row in property_matrix
    if row.get("current_month_statement_available") is True
    and (
        row.get("operational_status") == "blocked"
        or bool(row.get("automation_blocked_downloader_count"))
        or "resolve_configured_downloader_blocker" in (row.get("next_action_types") or [])
    )
]
report["mortgage_workflow_property_matrix_count"] = len(property_matrix)
report["mortgage_workflow_property_matrix_review_count"] = sum(
    1 for row in property_matrix if row.get("workflow_status") != "ready"
)
report["mortgage_workflow_property_matrix_manual_action_count"] = sum(
    row.get("manual_action_count") or 0 for row in property_matrix
)
report["mortgage_workflow_property_matrix_safe_action_count"] = sum(
    row.get("safe_action_count") or 0 for row in property_matrix
)
report["mortgage_workflow_property_matrix"] = property_matrix
report["coownership_tokenomics_status"] = report.get("tokenomics_status")
report["coownership_tokenomics_effective_status"] = report.get("tokenomics_effective_status")
report["coownership_tokenomics_current_month"] = report.get("tokenomics_current_month")
report["coownership_tokenomics_workbook_write_ready"] = (
    bool(property_matrix)
    and not workbook_write_blockers
    and report.get("tokenomics_safe_to_run_automatically") is not False
)
report["coownership_tokenomics_workbook_write_gate_status"] = (
    "ready" if report["coownership_tokenomics_workbook_write_ready"] else "review"
)
report["coownership_tokenomics_workbook_write_blocker_count"] = len(workbook_write_blockers)
report["coownership_tokenomics_workbook_write_blocker_properties"] = [
    item.get("property") for item in workbook_write_blockers if item.get("property")
]
report["coownership_tokenomics_workbook_write_blocker_reasons"] = workbook_write_blocker_reasons
report["coownership_tokenomics_workbook_write_blockers"] = workbook_write_blockers
report["coownership_tokenomics_blocking_capture_queue_count"] = len(tokenomics_blocking_capture_queue)
report["coownership_tokenomics_blocking_capture_queue_properties"] = [
    item.get("property") for item in tokenomics_blocking_capture_queue if item.get("property")
]
report["coownership_tokenomics_blocking_capture_queue"] = tokenomics_blocking_capture_queue
report["coownership_tokenomics_workbook_write_ready_property_count"] = len(workbook_write_ready_properties)
report["coownership_tokenomics_workbook_write_ready_properties"] = workbook_write_ready_properties
report["coownership_tokenomics_workbook_ready_property_write_plan_count"] = report.get(
    "tokenomics_workbook_ready_property_write_plan_count"
)
report["coownership_tokenomics_workbook_ready_property_write_plan_properties"] = report.get(
    "tokenomics_workbook_ready_property_write_plan_properties"
)
report["coownership_tokenomics_workbook_ready_property_write_plan"] = report.get(
    "tokenomics_workbook_ready_property_write_plan"
)
report["coownership_tokenomics_workbook_ready_property_planned_cell_write_count"] = report.get(
    "tokenomics_workbook_ready_property_planned_cell_write_count"
)
report["coownership_tokenomics_workbook_ready_property_write_blocked_by_full_gate"] = report.get(
    "tokenomics_workbook_ready_property_write_blocked_by_full_gate"
)
if report.get("tokenomics_workbook_write_is_live_for_this_run") is not True:
    report["tokenomics_workbook_write_metrics_suppressed_reason"] = "workbook-write-not-live-for-this-run"
    report["tokenomics_workbook_save_attempted"] = False
    report["tokenomics_workbook_partial_ready_property_write_attempted"] = False
    report["tokenomics_workbook_partial_ready_property_write_applied"] = False
    report["tokenomics_workbook_partial_ready_property_write_reason"] = (
        report.get("tokenomics_workbook_write_guard_reason") or "workbook-write-not-live-for-this-run"
    )
    report["tokenomics_workbook_partial_ready_property_write_property_count"] = 0
    report["tokenomics_workbook_partial_ready_property_write_properties"] = []
    report["tokenomics_balances_written"] = 0
    report["tokenomics_statement_reference_write_count"] = 0
    report["tokenomics_principal_balance_write_count"] = 0
    report["tokenomics_cell_write_count"] = 0
report["coownership_tokenomics_workbook_save_attempted"] = report.get("tokenomics_workbook_save_attempted")
report["coownership_tokenomics_workbook_partial_ready_property_write_attempted"] = report.get(
    "tokenomics_workbook_partial_ready_property_write_attempted"
)
report["coownership_tokenomics_workbook_partial_ready_property_write_applied"] = report.get(
    "tokenomics_workbook_partial_ready_property_write_applied"
)
report["coownership_tokenomics_workbook_partial_ready_property_write_reason"] = report.get(
    "tokenomics_workbook_partial_ready_property_write_reason"
)
report["coownership_tokenomics_workbook_partial_ready_property_write_property_count"] = report.get(
    "tokenomics_workbook_partial_ready_property_write_property_count"
)
report["coownership_tokenomics_workbook_partial_ready_property_write_properties"] = report.get(
    "tokenomics_workbook_partial_ready_property_write_properties"
)
report["coownership_tokenomics_workbook_partial_ready_property_cleared_copied_stale_property_count"] = report.get(
    "tokenomics_workbook_partial_ready_property_cleared_copied_stale_property_count"
)
report["coownership_tokenomics_workbook_partial_ready_property_cleared_copied_stale_properties"] = report.get(
    "tokenomics_workbook_partial_ready_property_cleared_copied_stale_properties"
)
report["mortgage_workflow_automation_blocked_with_current_statement_count"] = len(automation_blocked_with_current_statement)
report["mortgage_workflow_automation_blocked_with_current_statement_properties"] = [
    row.get("property") for row in automation_blocked_with_current_statement if row.get("property")
]
report["mortgage_workflow_automation_blocked_with_current_statement_records"] = [
    {
        "property": row.get("property"),
        "operational_status": row.get("operational_status"),
        "coverage_status": row.get("coverage_status"),
        "statement_month": row.get("statement_month"),
        "statement_month_status": row.get("statement_month_status"),
        "current_month_statement_available": row.get("current_month_statement_available"),
        "automation_blocked_downloader_count": row.get("automation_blocked_downloader_count"),
        "next_action_types": row.get("next_action_types"),
        "suggested_next_actions": row.get("suggested_next_actions"),
        "next_action_statuses": row.get("next_action_statuses"),
        "required_response_paths": row.get("required_response_paths"),
        "capture_commands": row.get("capture_commands"),
    }
    for row in automation_blocked_with_current_statement
]
report["mortgage_workflow_current_cycle_future_automation_attention_required"] = bool(
    automation_blocked_with_current_statement
)
report["mortgage_workflow_current_cycle_future_automation_attention_count"] = len(
    automation_blocked_with_current_statement
)
report["mortgage_workflow_current_cycle_future_automation_attention_properties"] = [
    row.get("property") for row in automation_blocked_with_current_statement if row.get("property")
]
workflow_future_automation_attention_reasons = []
for row in automation_blocked_with_current_statement:
    append_unique(
        workflow_future_automation_attention_reasons,
        row.get("next_action_reasons") or row.get("next_action_types") or [],
    )
report["mortgage_workflow_current_cycle_future_automation_attention_reasons"] = (
    workflow_future_automation_attention_reasons
)
report["mortgage_workflow_current_cycle_future_automation_attention_records"] = report[
    "mortgage_workflow_automation_blocked_with_current_statement_records"
]

def automation_attention_validation_command(item):
    if not isinstance(item, dict):
        return None
    downloader_id = item.get("id") or item.get("source_downloader_id")
    target_month = report.get("mortgage_statement_target_month")
    if downloader_id and target_month:
        return (
            f"MORTGAGE_DOWNLOADER_IDS={downloader_id} "
            f"MORTGAGE_STATEMENT_TARGET_MONTH={target_month} "
            "python3 scripts/run_mortgage_statement_downloaders.py"
        )
    report_path = item.get("report")
    if report_path:
        return f"test -s {report_path}"
    return None

def future_automation_repair_actions_from_attention(items):
    if not isinstance(items, list):
        return []
    actions = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        prop = item.get("property")
        reasons = item.get("reasons")
        if not isinstance(reasons, list):
            reasons = [item.get("reason")] if item.get("reason") else []
        row = {
            "property": prop,
            "servicer": item.get("servicer"),
            "source_downloader_id": item.get("id") or item.get("source_downloader_id"),
            "source_report": item.get("report"),
            "target_month": item.get("target_month") or report.get("mortgage_statement_target_month"),
            "expected_target_month": item.get("expected_target_month") or report.get("mortgage_statement_target_month"),
            "reasons": [reason for reason in reasons if reason not in (None, "", [], {})],
            "status": item.get("status"),
            "report_status": item.get("report_status"),
            "target_month_statement_available": item.get("target_month_statement_available"),
            "operator_next_action": item.get("operator_next_action"),
            "credential_lookup_candidate_count": item.get("credential_lookup_candidate_count"),
            "credential_lookup_unguarded_candidate_count": item.get("credential_lookup_unguarded_candidate_count"),
            "credential_lookup_misfiled_candidate_count": item.get("credential_lookup_misfiled_candidate_count"),
            "credential_lookup_absent": item.get("credential_lookup_absent"),
            "credential_lookup_scope": item.get("credential_lookup_scope"),
            "credential_lookup_repair_action": item.get("credential_lookup_repair_action"),
            "manual_auth_required": item.get("manual_auth_required"),
            "manual_auth_reason": item.get("manual_auth_reason"),
            "auth_failure_reason": item.get("auth_failure_reason"),
            "auth_failure_visible_reason": item.get("auth_failure_visible_reason"),
            "har_workflow_replay_blocker": item.get("har_workflow_replay_blocker"),
            "har_workflow_capture_quality_status": item.get("har_workflow_capture_quality_status"),
            "validation_command": automation_attention_validation_command(item),
        }
        row = {
            key: value
            for key, value in row.items()
            if value not in (None, "", {}) and (value != [] or key == "reasons")
        }
        marker = json.dumps(
            {
                "property": row.get("property"),
                "source_downloader_id": row.get("source_downloader_id"),
                "reasons": row.get("reasons"),
            },
            sort_keys=True,
        )
        if marker in seen:
            continue
        seen.add(marker)
        actions.append(row)
    return actions

def future_automation_repair_actions_from_matrix(items, existing_actions):
    if not isinstance(items, list):
        return []
    seen = set()
    for item in existing_actions:
        if not isinstance(item, dict):
            continue
        seen.add(json.dumps({"property": item.get("property"), "source": item.get("source")}, sort_keys=True))
    actions = []
    for item in items:
        if not isinstance(item, dict):
            continue
        prop = item.get("property")
        reasons = item.get("next_action_reasons")
        if not isinstance(reasons, list) or not reasons:
            reasons = item.get("next_action_types") if isinstance(item.get("next_action_types"), list) else []
        suggested = item.get("suggested_next_actions")
        if not isinstance(suggested, list):
            suggested = []
        capture_commands = item.get("capture_commands")
        if not isinstance(capture_commands, list):
            capture_commands = []
        post_capture_check_commands = item.get("post_capture_check_commands")
        if not isinstance(post_capture_check_commands, list):
            post_capture_check_commands = []
        row = {
            "property": prop,
            "servicer": item.get("servicer_hint"),
            "source": "property_matrix",
            "target_month": report.get("mortgage_statement_target_month"),
            "statement_month": item.get("statement_month"),
            "statement_month_status": item.get("statement_month_status"),
            "current_month_statement_available": item.get("current_month_statement_available"),
            "operational_status": item.get("operational_status"),
            "coverage_status": item.get("coverage_status"),
            "automation_blocked_downloader_count": item.get("automation_blocked_downloader_count"),
            "reasons": [reason for reason in reasons if reason not in (None, "", [], {})],
            "next_action_types": item.get("next_action_types"),
            "operator_next_action": suggested[0] if suggested else None,
            "suggested_next_actions": suggested,
            "credential_lookup_candidate_count": item.get("credential_lookup_candidate_count"),
            "credential_lookup_unguarded_candidate_count": item.get("credential_lookup_unguarded_candidate_count"),
            "credential_lookup_misfiled_candidate_count": item.get("credential_lookup_misfiled_candidate_count"),
            "credential_lookup_absent": item.get("credential_lookup_absent"),
            "credential_lookup_scope": item.get("credential_lookup_scope"),
            "credential_lookup_repair_action": item.get("credential_lookup_repair_action"),
            "capture_command": capture_commands[0] if capture_commands else None,
            "capture_commands": capture_commands,
            "required_response_paths": item.get("required_response_paths"),
            "validation_command": post_capture_check_commands[0] if post_capture_check_commands else None,
            "post_capture_check_commands": post_capture_check_commands,
        }
        row = {
            key: value
            for key, value in row.items()
            if value not in (None, "", {}) and (value != [] or key in {"reasons", "capture_commands", "post_capture_check_commands"})
        }
        marker = json.dumps({"property": row.get("property"), "source": row.get("source")}, sort_keys=True)
        if marker in seen:
            continue
        seen.add(marker)
        actions.append(row)
    return actions

future_automation_repair_actions = future_automation_repair_actions_from_attention(
    report.get("mortgage_downloader_automation_attention_details")
)
future_automation_repair_actions.extend(
    future_automation_repair_actions_from_matrix(
        automation_blocked_with_current_statement,
        future_automation_repair_actions,
    )
)
report["mortgage_workflow_future_automation_repair_action_count"] = len(
    future_automation_repair_actions
)
report["mortgage_workflow_future_automation_repair_properties"] = [
    item.get("property") for item in future_automation_repair_actions if item.get("property")
]
workflow_future_automation_repair_reasons = []
for item in future_automation_repair_actions:
    append_unique(workflow_future_automation_repair_reasons, item.get("reasons") or [])
report["mortgage_workflow_future_automation_repair_reasons"] = workflow_future_automation_repair_reasons
report["mortgage_workflow_future_automation_repair_actions"] = future_automation_repair_actions

def coverage_action_line(index, item):
    if not isinstance(item, dict):
        return None
    parts = [
        f"- {index}. type=`{item.get('type') or 'unknown'}`",
        f"property=`{item.get('property') or 'unknown'}`",
    ]
    if item.get("next_action_status"):
        parts.append(f"status=`{item.get('next_action_status')}`")
    if item.get("suggested_next_action"):
        parts.append(f"suggested_next_action=`{item.get('suggested_next_action')}`")
    if item.get("reason"):
        parts.append(f"reason=`{item.get('reason')}`")
    if item.get("skip_reason"):
        parts.append(f"skip_reason=`{item.get('skip_reason')}`")
    if item.get("target_statement_month"):
        parts.append(f"target_statement_month=`{item.get('target_statement_month')}`")
    target_details = item.get("target_statement_document_metadata_only_details")
    if isinstance(target_details, list) and target_details:
        first_target_detail = target_details[0] if isinstance(target_details[0], dict) else {}
        compact_detail = {
            key: first_target_detail.get(key)
            for key in ("date", "document_identifier", "statement_month", "name", "pdf_payload_status")
            if first_target_detail.get(key) not in (None, "", [], {})
        }
        if compact_detail:
            parts.append(f"target_statement_document=`{compact_detail}`")
    if item.get("target_statement_document_capture_instruction"):
        parts.append(
            f"target_statement_document_capture=`{item.get('target_statement_document_capture_instruction')}`"
        )
    for key, label in [
        ("capture_auth_blocker", "capture_auth_blocker"),
        ("capture_auth_blocker_reason", "capture_auth_blocker_reason"),
        ("capture_command_ready_to_run_now", "capture_command_ready_to_run_now"),
        ("capture_command_blocked_reason", "capture_command_blocked_reason"),
    ]:
        if item.get(key) is not None:
            parts.append(f"{label}=`{item.get(key)}`")
    if item.get("capture_auth_next_action"):
        parts.append(f"capture_auth_next_action=`{item.get('capture_auth_next_action')}`")
    for key, label in [
        ("capture_precondition_status", "capture_precondition_status"),
        ("capture_precondition_blocker", "capture_precondition_blocker"),
        ("capture_precondition_reason", "capture_precondition_reason"),
        ("capture_precondition_manual_auth_required", "capture_precondition_manual_auth_required"),
        ("capture_precondition_next_action", "capture_precondition_next_action"),
        ("capture_precondition_safe_to_capture_now", "capture_precondition_safe_to_capture_now"),
        ("capture_precondition_prepare_command", "capture_precondition_prepare_command"),
        ("capture_precondition_safe_to_prepare_now", "capture_precondition_safe_to_prepare_now"),
        ("credential_login_hint_mismatch", "credential_login_hint_mismatch"),
        ("credential_login_hint_mismatch_overridden", "credential_login_hint_mismatch_overridden"),
        ("credential_lookup_absent", "credential_absent"),
        ("credential_lookup_misfiled_candidate_count", "credential_misfiled_count"),
        ("credential_lookup_scope", "credential_scope"),
        ("credential_lookup_repair_action", "credential_repair_action"),
    ]:
        if item.get(key) is not None:
            parts.append(f"{label}=`{item.get(key)}`")
    for key, label in [
        ("evidence_sources", "evidence_sources"),
        ("reasons", "reasons"),
        ("body_recapture_recheck_missing_response_body_paths", "body_recapture_missing_paths"),
        ("tokenomics_write_blocker_reasons", "tokenomics_write_blocker_reasons"),
        ("tokenomics_write_blocker_statement_month_statuses", "tokenomics_write_blocker_statement_month_statuses"),
    ]:
        value = item.get(key)
        if isinstance(value, list) and value:
            parts.append(f"{label}=`{value}`")
    if item.get("tokenomics_workbook_write_blocking") is not None:
        parts.append(f"tokenomics_workbook_write_blocking=`{item.get('tokenomics_workbook_write_blocking')}`")
    if item.get("tokenomics_blocking") is not None:
        parts.append(f"tokenomics_blocking=`{item.get('tokenomics_blocking')}`")
    if item.get("workbook_write_blocking") is not None:
        parts.append(f"workbook_write_blocking=`{item.get('workbook_write_blocking')}`")
    if item.get("tokenomics_write_blocker_count") is not None:
        parts.append(f"tokenomics_write_blocker_count=`{item.get('tokenomics_write_blocker_count')}`")
    if item.get("body_recapture_recheck_response_body_requirement_count") is not None:
        parts.append(
            f"body_recapture_requirement_count=`{item.get('body_recapture_recheck_response_body_requirement_count')}`"
        )
    if item.get("body_recapture_operator_next_action"):
        parts.append(f"body_recapture_next_action=`{item.get('body_recapture_operator_next_action')}`")
    if item.get("body_recapture_validation_command"):
        parts.append(f"body_recapture_validation=`{item.get('body_recapture_validation_command')}`")
    if item.get("next_command"):
        parts.append(f"next_command=`{item.get('next_command')}`")
    if item.get("handoff_command"):
        parts.append(f"handoff_command=`{item.get('handoff_command')}`")
    if item.get("handoff_path"):
        parts.append(f"handoff_path=`{item.get('handoff_path')}`")
    if item.get("post_capture_check_command"):
        parts.append(f"post_capture_check=`{item.get('post_capture_check_command')}`")
    if item.get("validation_command"):
        parts.append(f"validation_command=`{item.get('validation_command')}`")
    if item.get("post_capture_check_script_path"):
        parts.append(f"post_capture_check_script=`{item.get('post_capture_check_script_path')}`")
    if item.get("register_workflow_evidence_command"):
        parts.append(f"register_workflow_evidence=`{item.get('register_workflow_evidence_command')}`")
    if item.get("register_workflow_evidence_apply_command"):
        parts.append(f"register_workflow_evidence_apply=`{item.get('register_workflow_evidence_apply_command')}`")
    if item.get("validate_existing_capture_command"):
        parts.append(f"validate_existing_capture=`{item.get('validate_existing_capture_command')}`")
    if item.get("advance_capture_workflow_evidence_dry_run_command"):
        parts.append(f"advance_capture_workflow_evidence_dry_run=`{item.get('advance_capture_workflow_evidence_dry_run_command')}`")
    if item.get("advance_capture_workflow_evidence_apply_command"):
        parts.append(f"advance_capture_workflow_evidence_apply=`{item.get('advance_capture_workflow_evidence_apply_command')}`")
    if item.get("advance_workflow_evidence_command"):
        parts.append(f"advance_workflow_evidence=`{item.get('advance_workflow_evidence_command')}`")
    if item.get("advance_command"):
        parts.append(f"advance_command=`{item.get('advance_command')}`")
    if item.get("advance_workflow_evidence_apply_command"):
        parts.append(f"advance_workflow_evidence_apply=`{item.get('advance_workflow_evidence_apply_command')}`")
    if item.get("advance_apply_command"):
        parts.append(f"advance_apply_command=`{item.get('advance_apply_command')}`")
    if item.get("analysis_command"):
        parts.append(f"analysis_command=`{item.get('analysis_command')}`")
    if item.get("scaffold_command"):
        parts.append(f"scaffold_command=`{item.get('scaffold_command')}`")
    if item.get("capture_command"):
        parts.append(f"capture_command=`{item.get('capture_command')}`")
    if item.get("report_path"):
        parts.append(f"analysis_report=`{Path(str(item.get('report_path'))).name}`")
    report_paths = item.get("report_paths")
    if isinstance(report_paths, list) and report_paths:
        parts.append(f"analysis_reports=`{[Path(str(path)).name for path in report_paths]}`")
    if item.get("analysis_report"):
        parts.append(f"analysis_report=`{Path(str(item.get('analysis_report'))).name}`")
    if item.get("scaffold_report"):
        parts.append(f"scaffold_report=`{Path(str(item.get('scaffold_report'))).name}`")
    if item.get("manifest_path"):
        parts.append(f"manifest=`{Path(str(item.get('manifest_path'))).name}`")
    if item.get("registry_entry_path"):
        parts.append(f"registry_entry=`{Path(str(item.get('registry_entry_path'))).name}`")
    if item.get("registry_entry_written") is not None:
        parts.append(f"registry_written=`{item.get('registry_entry_written')}`")
    if item.get("registry_install_command"):
        parts.append(f"registry_install_command=`{item.get('registry_install_command')}`")
    if item.get("generated_dry_run_command"):
        parts.append(f"generated_dry_run=`{item.get('generated_dry_run_command')}`")
    if item.get("generated_dry_run_status"):
        parts.append(f"generated_dry_run_status=`{item.get('generated_dry_run_status')}`")
    if item.get("generated_dry_run_verified_target_month") is not None:
        parts.append(f"generated_verified_target_month=`{item.get('generated_dry_run_verified_target_month')}`")
    if item.get("generated_dry_run_target_month_downloadable_count") is not None:
        parts.append(f"generated_downloadable=`{item.get('generated_dry_run_target_month_downloadable_count')}`")
    if item.get("safe_to_register_automatically") is not None:
        parts.append(f"safe_register=`{item.get('safe_to_register_automatically')}`")
    if item.get("suggested_workflow_har_path"):
        parts.append(f"har_path=`{item.get('suggested_workflow_har_path')}`")
    for key, label in [
        ("suggested_workflow_har_path_exists", "suggested_har_exists"),
        ("har_path_exists", "har_exists"),
        ("register_workflow_evidence_ready_to_run_now", "register_ready_now"),
        ("advance_workflow_evidence_ready_to_run_now", "advance_ready_now"),
        ("capture_required_before_offline_next_step", "capture_required_before_offline_next_step"),
        ("command_precondition_status", "command_precondition_status"),
        ("command_precondition_next_action", "command_precondition_next_action"),
        ("target_month_recapture_reason", "target_month_recapture_reason"),
        ("target_month_recapture_required", "target_month_recapture_required"),
    ]:
        if item.get(key) is not None:
            parts.append(f"{label}=`{item.get(key)}`")
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
            parts.append(f"target_candidate_pdf_statuses=`{statuses}`")
        if document_ids:
            parts.append(f"target_candidate_document_ids=`{document_ids}`")
    if item.get("har_path"):
        parts.append(f"har_path=`{item.get('har_path')}`")
    har_paths = item.get("har_paths")
    if isinstance(har_paths, list) and har_paths:
        parts.append(f"har_paths=`{har_paths}`")
    if item.get("capture_required") is not None:
        parts.append(f"capture_required=`{item.get('capture_required')}`")
    required_paths = item.get("required_response_paths")
    if isinstance(required_paths, list) and required_paths:
        parts.append(f"required_paths=`{required_paths}`")
    if item.get("portal_url"):
        parts.append(f"portal=`{item.get('portal_url')}`")
    return " ".join(parts)

def processor_action_lines(title, items):
    if not isinstance(items, list) or not items:
        return []
    lines = [
        f"### {title}",
        "",
    ]
    command_keys = [
        ("capture_prepare_command", "capture_prepare"),
        ("capture_command", "capture"),
        ("live_cdp_downloader_command", "live_cdp_downloader"),
        ("post_capture_apply_command", "post_capture_apply"),
        ("validation_command", "validation"),
    ]
    scalar_keys = [
        ("reason", "reason"),
        ("har_path", "har_path"),
        ("next_action_status", "status"),
        ("capture_auth_blocker", "capture_auth_blocker"),
        ("capture_auth_blocker_reason", "capture_auth_blocker_reason"),
        ("capture_command_ready_to_run_now", "capture_command_ready_to_run_now"),
        ("capture_command_blocked_reason", "capture_command_blocked_reason"),
        ("capture_auth_next_action", "capture_auth_next_action"),
        ("live_cdp_downloader_ready_after_visible_auth", "live_cdp_ready_after_visible_auth"),
        ("source_downloader_id", "source_downloader_id"),
        ("source_downloader_operator_next_action", "source_downloader_operator_next_action"),
        ("source_downloader_suggested_next_action", "source_downloader_suggested_next_action"),
        ("tokenomics_blocking", "tokenomics_blocking"),
        ("workbook_write_blocking", "workbook_write_blocking"),
        ("tokenomics_workbook_write_blocking", "tokenomics_workbook_write_blocking"),
    ]
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        parts = [
            f"- {index}. property=`{item.get('property') or 'unknown'}`",
        ]
        if item.get("next_action"):
            parts.append(f"next_action=`{item.get('next_action')}`")
        for key, label in scalar_keys:
            if item.get(key) is not None:
                parts.append(f"{label}=`{item.get(key)}`")
        lines.append(" ".join(parts))
        for key, label in command_keys:
            value = item.get(key)
            if value:
                lines.append(f"  - {label}: `{value}`")
        missing_paths = item.get("missing_response_body_paths")
        if isinstance(missing_paths, list) and missing_paths:
            lines.append(f"  - missing_response_body_paths: `{missing_paths}`")
        requirements = item.get("response_body_requirements")
        if isinstance(requirements, list) and requirements:
            for requirement in requirements:
                if not isinstance(requirement, dict):
                    continue
                lines.append(
                    "  - response_body_requirement: "
                    f"path=`{requirement.get('path')}` "
                    f"missing=`{requirement.get('missing_response_body_count')}` "
                    f"quality=`{requirement.get('required_capture_quality')}` "
                    f"roles=`{requirement.get('roles')}`"
                )
        progress_items = item.get("response_body_requirement_progress")
        if isinstance(progress_items, list) and progress_items:
            for progress in progress_items:
                if not isinstance(progress, dict):
                    continue
                lines.append(
                    "  - response_body_progress: "
                    f"path=`{progress.get('path')}` "
                    f"captured=`{progress.get('captured_count')}` "
                    f"missing=`{progress.get('missing_count')}` "
                    f"satisfied=`{progress.get('satisfied')}`"
                )
    lines.append("")
    return lines


def credential_repair_line(index, item):
    if not isinstance(item, dict):
        return None
    parts = [f"- credential_repair_{index}"]
    for key, label in [
        ("property", "property"),
        ("credential_repair_action", "action"),
        ("reason", "reason"),
        ("command_precondition_reason", "command_reason"),
        ("credential_lookup_status", "lookup_status"),
        ("credential_lookup_failure_reason", "lookup_failure"),
        ("credential_lookup_item_name", "item"),
        ("credential_lookup_expected_folder_name", "folder"),
        ("credential_lookup_uri_host", "host"),
        ("credential_lookup_search_terms", "search_terms"),
        ("credential_lookup_candidate_search_term_count", "candidate_search_term_count"),
        ("credential_lookup_candidate_search_terms", "candidate_search_terms"),
        ("credential_lookup_candidate_count", "candidate_count"),
        ("credential_lookup_candidate_items", "candidate_items"),
        ("credential_lookup_unguarded_candidate_count", "unguarded_candidate_count"),
        ("credential_lookup_unguarded_candidate_items", "unguarded_candidate_items"),
        ("credential_lookup_misfiled_candidate_count", "misfiled_candidate_count"),
        ("credential_lookup_absent", "credential_absent"),
        ("credential_lookup_scope", "credential_scope"),
        ("credential_lookup_repair_action", "credential_lookup_repair_action"),
        ("credential_login_hint_mismatch", "login_hint_mismatch"),
        ("credential_login_hint_mismatch_overridden", "login_hint_mismatch_overridden"),
        ("source_downloader_id", "downloader"),
        ("operator_next_action", "next_action"),
        ("credential_repair_validation_command", "validation"),
    ]:
        value = item.get(key)
        if value not in (None, "", {}) and (value != [] or key == "credential_lookup_candidate_items"):
            parts.append(f"{label}=`{value}`")
    return " ".join(parts)


def portal_auth_repair_line(index, item):
    if not isinstance(item, dict):
        return None
    parts = [f"- portal_auth_repair_{index}"]
    for key, label in [
        ("property", "property"),
        ("portal_auth_repair_action", "action"),
        ("reason", "reason"),
        ("command_precondition_reason", "command_reason"),
        ("capture_auth_blocker_reason", "capture_auth_blocker_reason"),
        ("capture_command_blocked_reason", "capture_command_blocked_reason"),
        ("auth_issue", "auth_issue"),
        ("manual_auth_reason", "manual_auth_reason"),
        ("auth_failure_reason", "auth_failure_reason"),
        ("auth_failure_visible_reason", "auth_failure_visible_reason"),
        ("credential_lookup_status", "lookup_status"),
        ("credential_lookup_item_name", "item"),
        ("credential_lookup_expected_folder_name", "folder"),
        ("credential_lookup_uri_host", "host"),
        ("credential_lookup_search_terms", "search_terms"),
        ("credential_lookup_candidate_search_term_count", "candidate_search_term_count"),
        ("credential_lookup_candidate_search_terms", "candidate_search_terms"),
        ("credential_lookup_candidate_count", "candidate_count"),
        ("credential_lookup_candidate_items", "candidate_items"),
        ("credential_lookup_unguarded_candidate_count", "unguarded_candidate_count"),
        ("credential_lookup_unguarded_candidate_items", "unguarded_candidate_items"),
        ("credential_lookup_misfiled_candidate_count", "misfiled_candidate_count"),
        ("credential_lookup_absent", "credential_absent"),
        ("credential_lookup_scope", "credential_scope"),
        ("credential_lookup_repair_action", "credential_lookup_repair_action"),
        ("credential_login_hint_mismatch", "login_hint_mismatch"),
        ("credential_login_hint_mismatch_overridden", "login_hint_mismatch_overridden"),
        ("source_downloader_id", "downloader"),
        ("operator_next_action", "next_action"),
        ("portal_auth_repair_validation_command", "validation"),
    ]:
        value = item.get(key)
        if value not in (None, "", {}) and (value != [] or key == "credential_lookup_candidate_items"):
            parts.append(f"{label}=`{value}`")
    return " ".join(parts)


def future_automation_repair_line(index, item):
    if not isinstance(item, dict):
        return None
    parts = [f"- future_automation_repair_{index}"]
    for key, label in [
        ("property", "property"),
        ("source", "source"),
        ("source_downloader_id", "downloader"),
        ("reasons", "reasons"),
        ("target_month", "target_month"),
        ("expected_target_month", "expected_target_month"),
        ("statement_month", "statement_month"),
        ("statement_month_status", "statement_month_status"),
        ("operational_status", "operational_status"),
        ("target_month_statement_available", "target_available"),
        ("current_month_statement_available", "current_statement_available"),
        ("manual_auth_required", "manual_auth_required"),
        ("manual_auth_reason", "manual_auth_reason"),
        ("auth_failure_reason", "auth_failure_reason"),
        ("har_workflow_replay_blocker", "har_replay_blocker"),
        ("har_workflow_capture_quality_status", "capture_quality"),
        ("credential_lookup_absent", "credential_absent"),
        ("credential_lookup_misfiled_candidate_count", "credential_misfiled_count"),
        ("credential_lookup_scope", "credential_scope"),
        ("credential_lookup_repair_action", "credential_repair_action"),
        ("operator_next_action", "next_action"),
        ("capture_command", "capture"),
        ("validation_command", "validation"),
    ]:
        value = item.get(key)
        if value not in (None, "", []):
            parts.append(f"{label}=`{value}`")
    return " ".join(parts)


def blocked_downloader_line(index, item):
    if not isinstance(item, dict):
        return None
    parts = [
        f"- {index}. property=`{item.get('property') or 'unknown'}`",
    ]
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
            parts.append(f"{label}=`{value}`")
    required_paths = item.get("required_response_paths")
    if isinstance(required_paths, list) and required_paths:
        parts.append(f"required_paths=`{required_paths}`")
    if item.get("capture_required") is not None:
        parts.append(f"capture_required=`{item.get('capture_required')}`")
    return " ".join(parts)

def tokenomics_gap_remediation_line(index, item):
    if not isinstance(item, dict):
        return None
    action = item.get("remediation_action")
    if not isinstance(action, dict):
        action = {}
    merged = dict(action)
    for key in ("property", "servicer_hint"):
        if item.get(key) and not merged.get(key):
            merged[key] = item.get(key)
    if item.get("remediation_reason") and not merged.get("reason"):
        merged["reason"] = item.get("remediation_reason")
    return coverage_action_line(index, merged)

def tokenomics_statement_line(index, item):
    if not isinstance(item, dict):
        return None
    parts = [
        f"- {index}. property=`{item.get('property') or 'unknown'}`",
        f"statement_found=`{item.get('statement_found')}`",
    ]
    if item.get("statement_filename"):
        parts.append(f"file=`{item.get('statement_filename')}`")
    if item.get("statement_date"):
        parts.append(f"statement_date=`{item.get('statement_date')}`")
    if item.get("statement_month_status"):
        parts.append(f"statement_month_status=`{item.get('statement_month_status')}`")
    if item.get("statement_month"):
        parts.append(f"statement_month=`{item.get('statement_month')}`")
    if item.get("current_month_statement_available") is not None:
        parts.append(f"current_month_statement_available=`{item.get('current_month_statement_available')}`")
    if item.get("statement_is_pdf") is not None:
        parts.append(f"pdf=`{item.get('statement_is_pdf')}`")
    if item.get("selected_directory_name"):
        parts.append(f"directory=`{item.get('selected_directory_name')}`")
    if item.get("statement_reference_cell"):
        parts.append(f"statement_cell=`{item.get('statement_reference_cell')}`")
    if item.get("statement_date_cell"):
        parts.append(f"date_cell=`{item.get('statement_date_cell')}`")
    if item.get("principal_balance_cell"):
        parts.append(f"balance_cell=`{item.get('principal_balance_cell')}`")
    if item.get("principal_balance_found") is not None:
        parts.append(f"principal_balance_found=`{item.get('principal_balance_found')}`")
    if item.get("write_skipped_reason"):
        parts.append(f"write_skipped_reason=`{item.get('write_skipped_reason')}`")
    return " ".join(parts)

def property_matrix_line(index, item):
    if not isinstance(item, dict):
        return None
    parts = [
        f"- {index}. property=`{item.get('property') or 'unknown'}`",
        f"workflow_status=`{item.get('workflow_status')}`",
        f"coverage=`{item.get('coverage_status')}`",
        f"statement_found=`{item.get('statement_found')}`",
        f"manual_actions=`{item.get('manual_action_count')}`",
        f"safe_actions=`{item.get('safe_action_count')}`",
    ]
    for key, label in [
        ("operational_status", "operational"),
        ("servicer_hint", "servicer"),
        ("missing_reason", "missing_reason"),
        ("latest_statement_filename", "latest_statement"),
        ("latest_statement_date", "latest_statement_date"),
        ("statement_month_status", "statement_month_status"),
        ("statement_month", "statement_month"),
        ("current_month_statement_available", "current_month_statement_available"),
        ("selected_directory_name", "directory"),
        ("next_action_types", "actions"),
        ("next_action_statuses", "statuses"),
        ("required_response_paths", "required_paths"),
        ("next_commands", "next_commands"),
        ("capture_commands", "capture_commands"),
        ("registry_entry_paths", "registry_entries"),
    ]:
        value = item.get(key)
        if value not in (None, "", []):
            parts.append(f"{label}=`{value}`")
    return " ".join(parts)

def workbook_blocker_line(index, item):
    if not isinstance(item, dict):
        return None
    parts = [
        f"- {index}. property=`{item.get('property') or 'unknown'}`",
        f"reasons=`{item.get('reasons')}`",
        f"workflow_status=`{item.get('workflow_status')}`",
        f"coverage=`{item.get('coverage_status')}`",
        f"operational=`{item.get('operational_status')}`",
        f"current_month_statement_available=`{item.get('current_month_statement_available')}`",
        f"statement_month_status=`{item.get('statement_month_status')}`",
    ]
    for key, label in [
        ("workflow_evidence_count", "workflow_evidence"),
        ("automation_blocked_downloader_count", "automation_blocked"),
        ("manual_action_count", "manual_actions"),
        ("next_action_types", "actions"),
        ("next_action_statuses", "statuses"),
        ("required_response_paths", "required_paths"),
        ("capture_commands", "capture_commands"),
        ("registry_entry_paths", "registry_entries"),
    ]:
        value = item.get(key)
        if value not in (None, "", []):
            parts.append(f"{label}=`{value}`")
    return " ".join(parts)

def tokenomics_blocking_capture_line(index, item):
    if not isinstance(item, dict):
        return None
    parts = [
        f"- {index}. property=`{item.get('property') or 'unknown'}`",
        f"reasons=`{item.get('reasons')}`",
        f"statement_month_status=`{item.get('statement_month_status')}`",
        f"statement_month=`{item.get('statement_month')}`",
        f"current_month_statement_available=`{item.get('current_month_statement_available')}`",
    ]
    for key, label in [
        ("workflow_status", "workflow_status"),
        ("coverage_status", "coverage"),
        ("operational_status", "operational"),
        ("capture_command", "capture_command"),
        ("post_capture_check_command", "post_capture_check_command"),
        ("required_response_paths", "required_paths"),
        ("manual_action_count", "manual_actions"),
        ("workflow_evidence_count", "workflow_evidence"),
    ]:
        value = item.get(key)
        if value not in (None, "", []):
            parts.append(f"{label}=`{value}`")
    return " ".join(parts)

def downloader_statement_freshness_line(index, item):
    if not isinstance(item, dict):
        return None
    latest_available = item.get("latest_statement_month")
    latest_downloadable = item.get("latest_downloadable_statement_month")
    if latest_available in (None, "") and latest_downloadable in (None, "") and not item.get("target_month_statement_available"):
        latest_available = "unknown"
    parts = [
        f"- {index}. property=`{item.get('property') or 'unknown'}`",
        f"status=`{item.get('report_status') or item.get('status')}`",
        f"target_month=`{item.get('target_month')}`",
        f"target_available=`{item.get('target_month_statement_available')}`",
        f"latest_available=`{latest_available}`",
        f"latest_downloadable=`{latest_downloadable}`",
    ]
    for key, label in [
        ("report_reason", "reason"),
        ("candidate_count", "candidate_count"),
        ("target_month_candidate_count", "target_candidates"),
        ("target_month_downloadable_count", "target_downloadable"),
        ("credentials_available", "credentials_available"),
        ("credential_source", "credential_source"),
        ("credential_lookup_status", "credential_lookup_status"),
        ("credential_lookup_failure_reason", "credential_lookup_failure_reason"),
        ("credential_lookup_item_name", "credential_lookup_item_name"),
        ("credential_lookup_expected_folder_name", "credential_lookup_expected_folder_name"),
        ("credential_lookup_uri_host", "credential_lookup_uri_host"),
        ("credential_login_hint_mismatch", "credential_login_hint_mismatch"),
        ("credential_login_hint_mismatch_overridden", "credential_login_hint_mismatch_overridden"),
        ("auth_issue", "auth_issue"),
        ("manual_auth_required", "manual_auth_required"),
        ("auth_state", "auth_state"),
    ]:
        value = item.get(key)
        if value not in (None, "", []):
            parts.append(f"{label}=`{value}`")
    months = item.get("downloadable_statement_months")
    if isinstance(months, list) and months:
        parts.append(f"downloadable_months=`{months}`")
    return " ".join(parts)

def repair_packet_markdown_lines(packet):
    lines = [
        "# Mortgage Workflow Repair Packet",
        "",
        f"- status: `{packet.get('status')}`",
        f"- reason: `{packet.get('reason')}`",
        f"- target_month: `{packet.get('target_month')}`",
        f"- action_count: `{packet.get('action_count')}`",
        f"- credential_repair_action_count: `{packet.get('credential_repair_action_count')}`",
        f"- portal_auth_repair_action_count: `{packet.get('portal_auth_repair_action_count')}`",
        f"- replayability_credential_repair_action_count: `{packet.get('replayability_credential_repair_action_count')}`",
        f"- replayability_portal_auth_repair_action_count: `{packet.get('replayability_portal_auth_repair_action_count')}`",
        f"- future_automation_repair_action_count: `{packet.get('future_automation_repair_action_count')}`",
        f"- capture_queue_action_count: `{packet.get('capture_queue_action_count')}`",
        f"- tokenomics_blocking_capture_count: `{packet.get('tokenomics_blocking_capture_count')}`",
        f"- safe_to_run_automatically: `{packet.get('safe_to_run_automatically')}`",
        "",
    ]
    sections = [
        ("Credential Repair", "credential_repair_actions", credential_repair_line),
        ("Portal Auth Repair", "portal_auth_repair_actions", portal_auth_repair_line),
        ("Replayability Credential Repair", "replayability_credential_repair_actions", credential_repair_line),
        ("Replayability Portal Auth Repair", "replayability_portal_auth_repair_actions", portal_auth_repair_line),
        ("Future Automation Repair", "future_automation_repair_actions", future_automation_repair_line),
        ("Tokenomics Blocking Captures", "tokenomics_blocking_captures", coverage_action_line),
        ("Capture Queue Actions", "capture_queue_actions", coverage_action_line),
    ]
    any_section = False
    for title, key, renderer in sections:
        items = packet.get(key)
        if not isinstance(items, list) or not items:
            continue
        any_section = True
        lines.extend([f"## {title}", ""])
        for index, item in enumerate(items, start=1):
            line = renderer(index, item)
            if line:
                lines.append(line)
        lines.append("")
    if not any_section:
        lines.append("- No repair actions are currently required.")
        lines.append("")
    lines.extend([
        "## Artifacts",
        "",
        f"- gate_report: `{packet.get('gate_report')}`",
        f"- repair_packet: `{packet.get('repair_packet')}`",
        f"- repair_packet_markdown: `{packet.get('repair_packet_markdown')}`",
        f"- capture_queue_report: `{packet.get('capture_queue_report')}`",
        f"- capture_queue_markdown: `{packet.get('capture_queue_markdown')}`",
        f"- capture_queue_processor_report: `{packet.get('capture_queue_processor_report')}`",
        f"- tokenomics_report: `{packet.get('tokenomics_report')}`",
        f"- coverage_report: `{packet.get('coverage_report')}`",
        "",
    ])
    return lines


repair_packet_path = report.get("repair_packet")
repair_packet_markdown_path = report.get("repair_packet_markdown")
credential_repair_actions = report.get("mortgage_workflow_credential_repair_actions")
portal_auth_repair_actions = report.get("mortgage_workflow_portal_auth_repair_actions")
replayability_credential_repair_actions = report.get("mortgage_workflow_replayability_credential_repair_actions")
replayability_portal_auth_repair_actions = report.get("mortgage_workflow_replayability_portal_auth_repair_actions")
capture_queue_actions = report.get("mortgage_workflow_capture_queue_actions")
tokenomics_blocking_captures = report.get("coownership_tokenomics_blocking_capture_queue")
future_automation_repair_actions = report.get("mortgage_workflow_future_automation_repair_actions")
credential_repair_actions = credential_repair_actions if isinstance(credential_repair_actions, list) else []
portal_auth_repair_actions = portal_auth_repair_actions if isinstance(portal_auth_repair_actions, list) else []
replayability_credential_repair_actions = (
    replayability_credential_repair_actions if isinstance(replayability_credential_repair_actions, list) else []
)
replayability_portal_auth_repair_actions = (
    replayability_portal_auth_repair_actions if isinstance(replayability_portal_auth_repair_actions, list) else []
)
capture_queue_actions = capture_queue_actions if isinstance(capture_queue_actions, list) else []
tokenomics_blocking_captures = tokenomics_blocking_captures if isinstance(tokenomics_blocking_captures, list) else []
future_automation_repair_actions = (
    future_automation_repair_actions if isinstance(future_automation_repair_actions, list) else []
)
repair_action_count = (
    len(credential_repair_actions)
    + len(portal_auth_repair_actions)
    + len(replayability_credential_repair_actions)
    + len(replayability_portal_auth_repair_actions)
    + len(future_automation_repair_actions)
    + len(capture_queue_actions)
    + len(tokenomics_blocking_captures)
)
repair_packet = {
    "status": "review" if repair_action_count else "ok",
    "reason": "mortgage_workflow_repair_actions_pending" if repair_action_count else "no_repair_actions_required",
    "stamp": report.get("stamp"),
    "target_month": report.get("mortgage_statement_target_month"),
    "report_only": report.get("report_only"),
    "safe_to_run_automatically": repair_action_count == 0,
    "action_count": repair_action_count,
    "credential_repair_action_count": len(credential_repair_actions),
    "credential_repair_properties": report.get("mortgage_workflow_credential_repair_properties") or [],
    "credential_repair_actions": credential_repair_actions,
    "portal_auth_repair_action_count": len(portal_auth_repair_actions),
    "portal_auth_repair_properties": report.get("mortgage_workflow_portal_auth_repair_properties") or [],
    "portal_auth_repair_actions": portal_auth_repair_actions,
    "replayability_credential_repair_action_count": len(replayability_credential_repair_actions),
    "replayability_credential_repair_properties": (
        report.get("mortgage_workflow_replayability_credential_repair_properties") or []
    ),
    "replayability_credential_repair_actions": replayability_credential_repair_actions,
    "replayability_portal_auth_repair_action_count": len(replayability_portal_auth_repair_actions),
    "replayability_portal_auth_repair_properties": (
        report.get("mortgage_workflow_replayability_portal_auth_repair_properties") or []
    ),
    "replayability_portal_auth_repair_actions": replayability_portal_auth_repair_actions,
    "future_automation_repair_action_count": len(future_automation_repair_actions),
    "future_automation_repair_properties": report.get("mortgage_workflow_future_automation_repair_properties") or [],
    "future_automation_repair_reasons": report.get("mortgage_workflow_future_automation_repair_reasons") or [],
    "future_automation_repair_actions": future_automation_repair_actions,
    "capture_queue_action_count": len(capture_queue_actions),
    "capture_queue_properties": report.get("mortgage_workflow_capture_queue_capture_properties") or [],
    "capture_queue_actions": capture_queue_actions,
    "tokenomics_blocking_capture_count": len(tokenomics_blocking_captures),
    "tokenomics_blocking_capture_properties": report.get("coownership_tokenomics_blocking_capture_queue_properties") or [],
    "tokenomics_blocking_captures": tokenomics_blocking_captures,
    "workbook_write_gate_status": report.get("coownership_tokenomics_workbook_write_gate_status"),
    "workbook_write_ready": report.get("coownership_tokenomics_workbook_write_ready"),
    "workbook_write_blocker_count": report.get("coownership_tokenomics_workbook_write_blocker_count"),
    "workbook_write_blocker_properties": report.get("coownership_tokenomics_workbook_write_blocker_properties") or [],
    "gate_report": os.environ.get("BASELANE_MORTGAGE_GATE_REPORT"),
    "repair_packet": repair_packet_path,
    "repair_packet_markdown": repair_packet_markdown_path,
    "capture_queue_report": report.get("mortgage_workflow_capture_queue_report"),
    "capture_queue_markdown": report.get("mortgage_workflow_capture_queue_markdown"),
    "capture_queue_processor_report": report.get("mortgage_workflow_capture_queue_processor_report"),
    "tokenomics_report": report.get("tokenomics_report"),
    "coverage_report": report.get("mortgage_downloader_coverage_report"),
}
repair_packet["idempotency_digest"] = stable_report_digest(repair_packet)
report["mortgage_workflow_repair_packet"] = repair_packet_path
report["mortgage_workflow_repair_packet_markdown"] = repair_packet_markdown_path
report["mortgage_workflow_repair_packet_status"] = repair_packet.get("status")
report["mortgage_workflow_repair_packet_reason"] = repair_packet.get("reason")
report["mortgage_workflow_repair_packet_action_count"] = repair_packet.get("action_count")
report["mortgage_workflow_repair_packet_idempotency_digest"] = repair_packet.get("idempotency_digest")
if repair_packet_path:
    write_json_if_changed(repair_packet_path, repair_packet)
if repair_packet_markdown_path:
    write_text_if_changed(
        repair_packet_markdown_path,
        "\n".join(repair_packet_markdown_lines(repair_packet)).rstrip() + "\n",
    )

packet_path = report.get("review_packet")
if packet_path:
    lines = [
        "# Baselane Mortgage Workflow Review",
        "",
        f"- status: `{report.get('status')}`",
        f"- reason: `{report.get('reason')}`",
        f"- stamp: `{report.get('stamp')}`",
        f"- mortgage_statement_target_month: `{report.get('mortgage_statement_target_month')}`",
        f"- report_only: `{report.get('report_only')}`",
        f"- live_stages_skipped: `{report.get('live_stages_skipped')}`",
        f"- downloader_rc: `{report.get('downloader_rc')}`",
        f"- mortgage_downloader_execution_status: `{report.get('mortgage_downloader_execution_status')}`",
        f"- mortgage_downloader_report_is_live_for_this_run: `{report.get('mortgage_downloader_report_is_live_for_this_run')}`",
        f"- citadel_download_rc: `{report.get('citadel_download_rc')}`",
        f"- mortgage_splits_rc: `{report.get('mortgage_splits_rc')}`",
        f"- mortgage_splits_report: `{report.get('mortgage_splits_report')}`",
        f"- mortgage_splits_status: `{report.get('mortgage_splits_status')}`",
        f"- mortgage_splits_classification: `{report.get('mortgage_splits_classification')}`",
        f"- mortgage_splits_issue_count: `{report.get('mortgage_splits_issue_count')}`",
        f"- mortgage_splits_safe_to_run_automatically: `{report.get('mortgage_splits_safe_to_run_automatically')}`",
        f"- no_mortgage_financials_guard_rc: `{report.get('no_mortgage_financials_guard_rc')}`",
        f"- no_mortgage_financials_guard_report: `{report.get('no_mortgage_financials_guard_report')}`",
        f"- no_mortgage_financials_guard_status: `{report.get('no_mortgage_financials_guard_status')}`",
        f"- no_mortgage_financials_guard_changed_file_count: `{report.get('no_mortgage_financials_guard_changed_file_count')}`",
        f"- no_mortgage_financials_guard_remaining_nonzero_count: `{report.get('no_mortgage_financials_guard_remaining_nonzero_count')}`",
        f"- no_mortgage_financials_guard_read_error_count: `{report.get('no_mortgage_financials_guard_read_error_count')}`",
        f"- tokenomics_rc: `{report.get('tokenomics_rc')}`",
        f"- tokenomics_workbook_write_is_live_for_this_run: `{report.get('tokenomics_workbook_write_is_live_for_this_run')}`",
        f"- tokenomics_workbook_write_guard_status: `{report.get('tokenomics_workbook_write_guard_status')}`",
        f"- tokenomics_workbook_write_guard_reason: `{report.get('tokenomics_workbook_write_guard_reason')}`",
        f"- mortgage_downloader_tokenomics_blocking_gap_count: `{report.get('mortgage_downloader_tokenomics_blocking_gap_count')}`",
        f"- mortgage_downloader_tokenomics_blocking_gap_properties: `{report.get('mortgage_downloader_tokenomics_blocking_gap_properties')}`",
        f"- mortgage_downloader_citadel_tokenomics_blocking: `{report.get('mortgage_downloader_citadel_tokenomics_blocking')}`",
        f"- tokenomics_report: `{report.get('tokenomics_report')}`",
        f"- tokenomics_status: `{report.get('tokenomics_status')}`",
        f"- tokenomics_effective_status: `{report.get('tokenomics_effective_status')}`",
        f"- tokenomics_current_month: `{report.get('tokenomics_current_month')}`",
        f"- tokenomics_mortgage_downloader_coverage_report: `{report.get('tokenomics_mortgage_downloader_coverage_report')}`",
        f"- tokenomics_mortgage_downloader_coverage_status: `{report.get('tokenomics_mortgage_downloader_coverage_status')}`",
        f"- tokenomics_mortgage_downloader_coverage_report_readable: `{report.get('tokenomics_mortgage_downloader_coverage_report_readable')}`",
        f"- tokenomics_workbook_save_attempted: `{report.get('tokenomics_workbook_save_attempted')}`",
        f"- tokenomics_created_sheet: `{report.get('tokenomics_created_sheet')}`",
        f"- tokenomics_property_column_count: `{report.get('tokenomics_property_column_count')}`",
        f"- tokenomics_co_owner_paid_property_count: `{report.get('tokenomics_co_owner_paid_property_count')}`",
        f"- tokenomics_co_owner_paid_property_alignment_status: `{report.get('tokenomics_co_owner_paid_property_alignment_status')}`",
        f"- tokenomics_co_owner_paid_property_alignment_mismatch_count: `{report.get('tokenomics_co_owner_paid_property_alignment_mismatch_count')}`",
        f"- tokenomics_co_owner_paid_property_alignment_missing_from_coverage: `{report.get('tokenomics_co_owner_paid_property_alignment_missing_from_coverage')}`",
        f"- tokenomics_co_owner_paid_property_alignment_missing_from_tokenomics: `{report.get('tokenomics_co_owner_paid_property_alignment_missing_from_tokenomics')}`",
        f"- tokenomics_skipped_non_co_owner_paid_property_count: `{report.get('tokenomics_skipped_non_co_owner_paid_property_count')}`",
        f"- tokenomics_statement_found_count: `{report.get('tokenomics_statement_found_count')}`",
        f"- tokenomics_current_month_statement_found_count: `{report.get('tokenomics_current_month_statement_found_count')}`",
        f"- tokenomics_current_month_statement_gap_count: `{report.get('tokenomics_current_month_statement_gap_count')}`",
        f"- tokenomics_current_month_statement_gap_properties: `{report.get('tokenomics_current_month_statement_gap_properties')}`",
        f"- tokenomics_current_month_statement_gap_remediation_count: `{report.get('tokenomics_current_month_statement_gap_remediation_count')}`",
        f"- tokenomics_current_month_statement_gap_remediation_available_count: `{report.get('tokenomics_current_month_statement_gap_remediation_available_count')}`",
        f"- tokenomics_current_month_statement_gap_remediation_missing_action_count: `{report.get('tokenomics_current_month_statement_gap_remediation_missing_action_count')}`",
        f"- tokenomics_current_month_statement_gap_remediation_missing_action_properties: `{report.get('tokenomics_current_month_statement_gap_remediation_missing_action_properties')}`",
        f"- tokenomics_archive_statement_candidate_count: `{report.get('tokenomics_archive_statement_candidate_count')}`",
        f"- tokenomics_archive_statement_candidate_properties: `{report.get('tokenomics_archive_statement_candidate_properties')}`",
        f"- tokenomics_archive_current_month_statement_candidate_count: `{report.get('tokenomics_archive_current_month_statement_candidate_count')}`",
        f"- tokenomics_archive_current_month_statement_candidate_properties: `{report.get('tokenomics_archive_current_month_statement_candidate_properties')}`",
        f"- tokenomics_stale_statement_count: `{report.get('tokenomics_stale_statement_count')}`",
        f"- tokenomics_stale_statement_properties: `{report.get('tokenomics_stale_statement_properties')}`",
        f"- tokenomics_missing_current_month_statement_count: `{report.get('tokenomics_missing_current_month_statement_count')}`",
        f"- tokenomics_missing_current_month_statement_properties: `{report.get('tokenomics_missing_current_month_statement_properties')}`",
        f"- tokenomics_pdf_statement_count: `{report.get('tokenomics_pdf_statement_count')}`",
        f"- tokenomics_statements_found: `{report.get('tokenomics_statements_found')}`",
        f"- tokenomics_balances_written: `{report.get('tokenomics_balances_written')}`",
        f"- tokenomics_statement_reference_write_count: `{report.get('tokenomics_statement_reference_write_count')}`",
        f"- tokenomics_principal_balance_write_count: `{report.get('tokenomics_principal_balance_write_count')}`",
        f"- tokenomics_cell_write_count: `{report.get('tokenomics_cell_write_count')}`",
        f"- tokenomics_safe_to_run_automatically: `{report.get('tokenomics_safe_to_run_automatically')}`",
        f"- tokenomics_statement_record_count: `{report.get('tokenomics_statement_record_count')}`",
        f"- tokenomics_co_owner_paid_statement_record_count: `{report.get('tokenomics_co_owner_paid_statement_record_count')}`",
        f"- tokenomics_missing_co_owner_paid_statement_properties: `{report.get('tokenomics_missing_co_owner_paid_statement_properties')}`",
        f"- coownership_tokenomics_status: `{report.get('coownership_tokenomics_status')}`",
        f"- coownership_tokenomics_effective_status: `{report.get('coownership_tokenomics_effective_status')}`",
        f"- coownership_tokenomics_current_month: `{report.get('coownership_tokenomics_current_month')}`",
        f"- coownership_tokenomics_workbook_write_gate_status: `{report.get('coownership_tokenomics_workbook_write_gate_status')}`",
        f"- coownership_tokenomics_workbook_write_ready: `{report.get('coownership_tokenomics_workbook_write_ready')}`",
        f"- coownership_tokenomics_workbook_write_blocker_count: `{report.get('coownership_tokenomics_workbook_write_blocker_count')}`",
        f"- coownership_tokenomics_workbook_write_blocker_properties: `{report.get('coownership_tokenomics_workbook_write_blocker_properties')}`",
        f"- coownership_tokenomics_workbook_write_blocker_reasons: `{report.get('coownership_tokenomics_workbook_write_blocker_reasons')}`",
        f"- coownership_tokenomics_blocking_capture_queue_count: `{report.get('coownership_tokenomics_blocking_capture_queue_count')}`",
        f"- coownership_tokenomics_blocking_capture_queue_properties: `{report.get('coownership_tokenomics_blocking_capture_queue_properties')}`",
        f"- coownership_tokenomics_workbook_write_ready_property_count: `{report.get('coownership_tokenomics_workbook_write_ready_property_count')}`",
        f"- coownership_tokenomics_workbook_write_ready_properties: `{report.get('coownership_tokenomics_workbook_write_ready_properties')}`",
        f"- coownership_tokenomics_workbook_ready_property_write_plan_count: `{report.get('coownership_tokenomics_workbook_ready_property_write_plan_count')}`",
        f"- coownership_tokenomics_workbook_ready_property_write_plan_properties: `{report.get('coownership_tokenomics_workbook_ready_property_write_plan_properties')}`",
        f"- coownership_tokenomics_workbook_ready_property_planned_cell_write_count: `{report.get('coownership_tokenomics_workbook_ready_property_planned_cell_write_count')}`",
        f"- coownership_tokenomics_workbook_ready_property_write_blocked_by_full_gate: `{report.get('coownership_tokenomics_workbook_ready_property_write_blocked_by_full_gate')}`",
        f"- coownership_tokenomics_workbook_partial_ready_property_write_attempted: `{report.get('coownership_tokenomics_workbook_partial_ready_property_write_attempted')}`",
        f"- coownership_tokenomics_workbook_partial_ready_property_write_applied: `{report.get('coownership_tokenomics_workbook_partial_ready_property_write_applied')}`",
        f"- coownership_tokenomics_workbook_partial_ready_property_write_reason: `{report.get('coownership_tokenomics_workbook_partial_ready_property_write_reason')}`",
        f"- coownership_tokenomics_workbook_partial_ready_property_write_property_count: `{report.get('coownership_tokenomics_workbook_partial_ready_property_write_property_count')}`",
        f"- coownership_tokenomics_workbook_partial_ready_property_write_properties: `{report.get('coownership_tokenomics_workbook_partial_ready_property_write_properties')}`",
        f"- coownership_tokenomics_workbook_partial_ready_property_cleared_copied_stale_property_count: `{report.get('coownership_tokenomics_workbook_partial_ready_property_cleared_copied_stale_property_count')}`",
        f"- coownership_tokenomics_workbook_partial_ready_property_cleared_copied_stale_properties: `{report.get('coownership_tokenomics_workbook_partial_ready_property_cleared_copied_stale_properties')}`",
        f"- mortgage_downloader_coverage_rc: `{report.get('mortgage_downloader_coverage_rc')}`",
        f"- mortgage_downloader_coverage_report: `{report.get('mortgage_downloader_coverage_report')}`",
        f"- mortgage_downloader_coverage_status: `{report.get('mortgage_downloader_coverage_status')}`",
        f"- mortgage_downloader_coverage_tokenomics_source: `{report.get('mortgage_downloader_coverage_tokenomics_source')}`",
        f"- mortgage_downloader_coverage_tokenomics_fallback_report_used: `{report.get('mortgage_downloader_coverage_tokenomics_fallback_report_used')}`",
        f"- mortgage_downloader_coverage_tokenomics_fallback_report: `{report.get('mortgage_downloader_coverage_tokenomics_fallback_report')}`",
        f"- mortgage_downloader_coverage_tokenomics_fallback_error: `{report.get('mortgage_downloader_coverage_tokenomics_fallback_error')}`",
        f"- mortgage_downloader_coverage_co_owner_paid_property_count: `{report.get('mortgage_downloader_coverage_co_owner_paid_property_count')}`",
        f"- mortgage_downloader_coverage_covered_co_owner_paid_property_count: `{report.get('mortgage_downloader_coverage_covered_co_owner_paid_property_count')}`",
        f"- mortgage_downloader_coverage_automation_ready_co_owner_paid_property_count: `{report.get('mortgage_downloader_coverage_automation_ready_co_owner_paid_property_count')}`",
        f"- mortgage_downloader_coverage_configured_but_not_ready_co_owner_paid_property_count: `{report.get('mortgage_downloader_coverage_configured_but_not_ready_co_owner_paid_property_count')}`",
        f"- mortgage_downloader_coverage_configured_downloader_count: `{report.get('mortgage_downloader_coverage_configured_downloader_count')}`",
        f"- mortgage_downloader_coverage_configured_downloader_properties: `{report.get('mortgage_downloader_coverage_configured_downloader_properties')}`",
        f"- mortgage_downloader_coverage_configured_downloader_latest_report_statuses: `{report.get('mortgage_downloader_coverage_configured_downloader_latest_report_statuses')}`",
        f"- mortgage_downloader_coverage_configured_downloader_latest_report_reasons: `{report.get('mortgage_downloader_coverage_configured_downloader_latest_report_reasons')}`",
        f"- mortgage_downloader_coverage_configured_downloader_blocker_reasons: `{report.get('mortgage_downloader_coverage_configured_downloader_blocker_reasons')}`",
        f"- mortgage_downloader_coverage_active_configured_downloader_count: `{report.get('mortgage_downloader_coverage_active_configured_downloader_count')}`",
        f"- mortgage_downloader_coverage_active_configured_downloader_ids: `{report.get('mortgage_downloader_coverage_active_configured_downloader_ids')}`",
        f"- mortgage_downloader_coverage_automation_ready_configured_downloader_count: `{report.get('mortgage_downloader_coverage_automation_ready_configured_downloader_count')}`",
        f"- mortgage_downloader_coverage_automation_ready_configured_downloader_ids: `{report.get('mortgage_downloader_coverage_automation_ready_configured_downloader_ids')}`",
        f"- mortgage_downloader_coverage_automation_blocked_configured_downloader_count: `{report.get('mortgage_downloader_coverage_automation_blocked_configured_downloader_count')}`",
        f"- mortgage_downloader_coverage_automation_blocked_configured_downloader_ids: `{report.get('mortgage_downloader_coverage_automation_blocked_configured_downloader_ids')}`",
        f"- mortgage_downloader_coverage_active_downloader_mode_counts: `{report.get('mortgage_downloader_coverage_active_downloader_mode_counts')}`",
        f"- mortgage_downloader_coverage_automation_ready_downloader_mode_counts: `{report.get('mortgage_downloader_coverage_automation_ready_downloader_mode_counts')}`",
        f"- mortgage_downloader_coverage_active_har_backed_offline_extractor_count: `{report.get('mortgage_downloader_coverage_active_har_backed_offline_extractor_count')}`",
        f"- mortgage_downloader_coverage_automation_ready_har_backed_offline_extractor_count: `{report.get('mortgage_downloader_coverage_automation_ready_har_backed_offline_extractor_count')}`",
        f"- mortgage_downloader_coverage_active_live_cdp_portal_downloader_count: `{report.get('mortgage_downloader_coverage_active_live_cdp_portal_downloader_count')}`",
        f"- mortgage_downloader_coverage_automation_ready_live_cdp_portal_downloader_count: `{report.get('mortgage_downloader_coverage_automation_ready_live_cdp_portal_downloader_count')}`",
        f"- mortgage_downloader_coverage_current_month_statement_available_count: `{report.get('mortgage_downloader_coverage_current_month_statement_available_count')}`",
        f"- mortgage_downloader_coverage_current_month_statement_gap_count: `{report.get('mortgage_downloader_coverage_current_month_statement_gap_count')}`",
        f"- mortgage_downloader_coverage_current_month_statement_gap_properties: `{report.get('mortgage_downloader_coverage_current_month_statement_gap_properties')}`",
        f"- mortgage_downloader_coverage_downloader_target_month_statement_gap_count: `{report.get('mortgage_downloader_coverage_downloader_target_month_statement_gap_count')}`",
        f"- mortgage_downloader_coverage_downloader_target_month_statement_gap_properties: `{report.get('mortgage_downloader_coverage_downloader_target_month_statement_gap_properties')}`",
        f"- mortgage_downloader_coverage_stale_statement_count: `{report.get('mortgage_downloader_coverage_stale_statement_count')}`",
        f"- mortgage_downloader_coverage_stale_statement_properties: `{report.get('mortgage_downloader_coverage_stale_statement_properties')}`",
        f"- mortgage_downloader_coverage_missing_current_month_statement_count: `{report.get('mortgage_downloader_coverage_missing_current_month_statement_count')}`",
        f"- mortgage_downloader_coverage_missing_current_month_statement_properties: `{report.get('mortgage_downloader_coverage_missing_current_month_statement_properties')}`",
        f"- mortgage_downloader_coverage_blocked_configured_downloader_count: `{report.get('mortgage_downloader_coverage_blocked_configured_downloader_count')}`",
        f"- mortgage_downloader_coverage_blocked_configured_downloader_properties: `{report.get('mortgage_downloader_coverage_blocked_configured_downloader_properties')}`",
        f"- mortgage_downloader_coverage_automation_repair_not_statement_gap_count: `{report.get('mortgage_downloader_coverage_automation_repair_not_statement_gap_count')}`",
        f"- mortgage_downloader_coverage_automation_repair_not_statement_gap_properties: `{report.get('mortgage_downloader_coverage_automation_repair_not_statement_gap_properties')}`",
        f"- mortgage_downloader_coverage_missing_downloader_count: `{report.get('mortgage_downloader_coverage_missing_downloader_count')}`",
        f"- mortgage_downloader_coverage_missing_downloader_properties: `{report.get('mortgage_downloader_coverage_missing_downloader_properties')}`",
        f"- mortgage_downloader_coverage_workflow_evidence_capture_plan_count: `{report.get('mortgage_downloader_coverage_workflow_evidence_capture_plan_count')}`",
        f"- mortgage_downloader_coverage_next_action_plan_count: `{report.get('mortgage_downloader_coverage_next_action_plan_count')}`",
        f"- mortgage_downloader_coverage_next_action_manual_count: `{report.get('mortgage_downloader_coverage_next_action_manual_count')}`",
        f"- mortgage_downloader_coverage_next_action_safe_to_run_automatically_count: `{report.get('mortgage_downloader_coverage_next_action_safe_to_run_automatically_count')}`",
        f"- mortgage_downloader_coverage_safe_to_run_automatically: `{report.get('mortgage_downloader_coverage_safe_to_run_automatically')}`",
        f"- mortgage_workflow_replayability_rc: `{report.get('mortgage_workflow_replayability_rc')}`",
        f"- mortgage_workflow_replayability_report: `{report.get('mortgage_workflow_replayability_report')}`",
        f"- mortgage_workflow_replayability_status: `{report.get('mortgage_workflow_replayability_status')}`",
        f"- mortgage_workflow_replayability_reason: `{report.get('mortgage_workflow_replayability_reason')}`",
        f"- mortgage_workflow_replayability_safe_to_run_automatically: `{report.get('mortgage_workflow_replayability_safe_to_run_automatically')}`",
        f"- mortgage_workflow_replayability_har_analysis_count: `{report.get('mortgage_workflow_replayability_har_analysis_count')}`",
        f"- mortgage_workflow_replayability_har_analysis_blocked_count: `{report.get('mortgage_workflow_replayability_har_analysis_blocked_count')}`",
        f"- mortgage_workflow_replayability_raw_discovered_har_candidate_property_count: `{report.get('mortgage_workflow_replayability_raw_discovered_har_candidate_property_count')}`",
        f"- mortgage_workflow_replayability_discovered_har_candidate_property_count: `{report.get('mortgage_workflow_replayability_discovered_har_candidate_property_count')}`",
        f"- mortgage_workflow_replayability_safe_discovered_har_candidate_property_count: `{report.get('mortgage_workflow_replayability_safe_discovered_har_candidate_property_count')}`",
        f"- mortgage_workflow_replayability_rejected_discovered_har_candidate_count: `{report.get('mortgage_workflow_replayability_rejected_discovered_har_candidate_count')}`",
        f"- mortgage_workflow_replayability_rejected_discovered_har_candidate_paths: `{report.get('mortgage_workflow_replayability_rejected_discovered_har_candidate_paths')}`",
        f"- mortgage_workflow_replayability_next_action_plan_count: `{report.get('mortgage_workflow_replayability_next_action_plan_count')}`",
        f"- mortgage_workflow_replayability_next_action_manual_count: `{report.get('mortgage_workflow_replayability_next_action_manual_count')}`",
        f"- mortgage_workflow_evidence_advance_rc: `{report.get('mortgage_workflow_evidence_advance_rc')}`",
        f"- mortgage_workflow_evidence_advance_report: `{report.get('mortgage_workflow_evidence_advance_report')}`",
        f"- mortgage_workflow_evidence_advance_status: `{report.get('mortgage_workflow_evidence_advance_status')}`",
        f"- mortgage_workflow_evidence_advance_reason: `{report.get('mortgage_workflow_evidence_advance_reason')}`",
        f"- mortgage_workflow_evidence_advance_ok_count: `{report.get('mortgage_workflow_evidence_advance_ok_count')}`",
        f"- mortgage_workflow_evidence_advance_review_count: `{report.get('mortgage_workflow_evidence_advance_review_count')}`",
        f"- mortgage_workflow_evidence_advance_safe_to_run_automatically: `{report.get('mortgage_workflow_evidence_advance_safe_to_run_automatically')}`",
        f"- mortgage_workflow_evidence_advance_enable_verified_registry_entry: `{report.get('mortgage_workflow_evidence_advance_enable_verified_registry_entry')}`",
        f"- mortgage_workflow_evidence_refresh_rc: `{report.get('mortgage_workflow_evidence_refresh_rc')}`",
        f"- mortgage_workflow_evidence_refresh_report: `{report.get('mortgage_workflow_evidence_refresh_report')}`",
        f"- mortgage_workflow_evidence_refresh_idempotency_digest: `{report.get('mortgage_workflow_evidence_refresh_idempotency_digest')}`",
        f"- mortgage_workflow_evidence_refresh_status: `{report.get('mortgage_workflow_evidence_refresh_status')}`",
        f"- mortgage_workflow_evidence_refresh_reason: `{report.get('mortgage_workflow_evidence_refresh_reason')}`",
        f"- mortgage_workflow_evidence_refresh_analysis_ready_count: `{report.get('mortgage_workflow_evidence_refresh_analysis_ready_count')}`",
        f"- mortgage_workflow_evidence_refresh_analysis_blocked_count: `{report.get('mortgage_workflow_evidence_refresh_analysis_blocked_count')}`",
        f"- mortgage_workflow_evidence_refresh_scaffold_ready_count: `{report.get('mortgage_workflow_evidence_refresh_scaffold_ready_count')}`",
        f"- mortgage_workflow_evidence_refresh_ensure_handoffs: `{report.get('mortgage_workflow_evidence_refresh_ensure_handoffs')}`",
        f"- mortgage_workflow_evidence_refresh_handoff_pending_property_count: `{report.get('mortgage_workflow_evidence_refresh_handoff_pending_property_count')}`",
        f"- mortgage_workflow_evidence_refresh_handoff_file_gap_count: `{report.get('mortgage_workflow_evidence_refresh_handoff_file_gap_count')}`",
        f"- mortgage_workflow_evidence_refresh_har_path_missing_count: `{report.get('mortgage_workflow_evidence_refresh_har_path_missing_count')}`",
        f"- mortgage_workflow_evidence_refresh_har_path_present_blocked_count: `{report.get('mortgage_workflow_evidence_refresh_har_path_present_blocked_count')}`",
        f"- mortgage_workflow_evidence_refresh_raw_discovered_har_candidate_property_count: `{report.get('mortgage_workflow_evidence_refresh_raw_discovered_har_candidate_property_count')}`",
        f"- mortgage_workflow_evidence_refresh_discovered_har_candidate_property_count: `{report.get('mortgage_workflow_evidence_refresh_discovered_har_candidate_property_count')}`",
        f"- mortgage_workflow_evidence_refresh_safe_discovered_har_candidate_property_count: `{report.get('mortgage_workflow_evidence_refresh_safe_discovered_har_candidate_property_count')}`",
        f"- mortgage_workflow_evidence_refresh_rejected_discovered_har_candidate_count: `{report.get('mortgage_workflow_evidence_refresh_rejected_discovered_har_candidate_count')}`",
        f"- mortgage_workflow_evidence_refresh_rejected_discovered_har_candidate_paths: `{report.get('mortgage_workflow_evidence_refresh_rejected_discovered_har_candidate_paths')}`",
        f"- mortgage_workflow_evidence_refresh_post_capture_check_ready_count: `{report.get('mortgage_workflow_evidence_refresh_post_capture_check_ready_count')}`",
        f"- mortgage_workflow_evidence_refresh_capture_required_before_offline_next_step_count: `{report.get('mortgage_workflow_evidence_refresh_capture_required_before_offline_next_step_count')}`",
        f"- mortgage_workflow_evidence_refresh_capture_required_before_offline_next_step_properties: `{report.get('mortgage_workflow_evidence_refresh_capture_required_before_offline_next_step_properties')}`",
        f"- mortgage_workflow_evidence_refresh_offline_next_step_ready_count: `{report.get('mortgage_workflow_evidence_refresh_offline_next_step_ready_count')}`",
        f"- mortgage_workflow_evidence_refresh_offline_next_step_gap_count: `{report.get('mortgage_workflow_evidence_refresh_offline_next_step_gap_count')}`",
        f"- mortgage_workflow_evidence_refresh_next_action_plan_count: `{report.get('mortgage_workflow_evidence_refresh_next_action_plan_count')}`",
        f"- mortgage_workflow_evidence_refresh_next_action_manual_count: `{report.get('mortgage_workflow_evidence_refresh_next_action_manual_count')}`",
        f"- mortgage_workflow_evidence_refresh_next_action_safe_to_run_automatically_count: `{report.get('mortgage_workflow_evidence_refresh_next_action_safe_to_run_automatically_count')}`",
        f"- mortgage_workflow_evidence_refresh_safe_to_run_automatically: `{report.get('mortgage_workflow_evidence_refresh_safe_to_run_automatically')}`",
        f"- mortgage_workflow_evidence_refresh_stale_scaffold_cleanup_count: `{report.get('mortgage_workflow_evidence_refresh_stale_scaffold_cleanup_count')}`",
        f"- mortgage_workflow_evidence_refresh_stale_scaffold_cleanup_properties: `{report.get('mortgage_workflow_evidence_refresh_stale_scaffold_cleanup_properties')}`",
        f"- mortgage_workflow_evidence_refresh_stale_scaffold_cleanup_paths: `{report.get('mortgage_workflow_evidence_refresh_stale_scaffold_cleanup_paths')}`",
        f"- mortgage_workflow_capture_queue_rc: `{report.get('mortgage_workflow_capture_queue_rc')}`",
        f"- mortgage_workflow_capture_queue_report: `{report.get('mortgage_workflow_capture_queue_report')}`",
        f"- mortgage_workflow_capture_queue_markdown: `{report.get('mortgage_workflow_capture_queue_markdown')}`",
        f"- mortgage_workflow_capture_queue_idempotency_digest: `{report.get('mortgage_workflow_capture_queue_idempotency_digest')}`",
        f"- mortgage_workflow_capture_queue_status: `{report.get('mortgage_workflow_capture_queue_status')}`",
        f"- mortgage_workflow_capture_queue_candidate_capture_handoff_count: `{report.get('mortgage_workflow_capture_queue_candidate_capture_handoff_count')}`",
        f"- mortgage_workflow_capture_queue_handoff_candidate_capture_count: `{report.get('mortgage_workflow_capture_queue_handoff_candidate_capture_count')}`",
        f"- mortgage_workflow_capture_queue_downloader_gap_candidate_capture_count: `{report.get('mortgage_workflow_capture_queue_downloader_gap_candidate_capture_count')}`",
        f"- mortgage_workflow_capture_queue_downloader_gap_candidate_capture_properties: `{report.get('mortgage_workflow_capture_queue_downloader_gap_candidate_capture_properties')}`",
        f"- mortgage_workflow_capture_queue_downloader_report: `{report.get('mortgage_workflow_capture_queue_downloader_report')}`",
        f"- mortgage_workflow_capture_queue_downloader_report_status: `{report.get('mortgage_workflow_capture_queue_downloader_report_status')}`",
        f"- mortgage_workflow_capture_queue_capture_count: `{report.get('mortgage_workflow_capture_queue_capture_count')}`",
        f"- mortgage_workflow_capture_queue_capture_properties: `{report.get('mortgage_workflow_capture_queue_capture_properties')}`",
        f"- mortgage_workflow_capture_queue_duplicate_capture_handoff_count: `{report.get('mortgage_workflow_capture_queue_duplicate_capture_handoff_count')}`",
        f"- mortgage_workflow_capture_queue_duplicate_capture_handoff_properties: `{report.get('mortgage_workflow_capture_queue_duplicate_capture_handoff_properties')}`",
        f"- mortgage_workflow_capture_queue_duplicate_capture_handoffs_suppressed: `{report.get('mortgage_workflow_capture_queue_duplicate_capture_handoffs_suppressed')}`",
        f"- mortgage_workflow_capture_queue_missing_har_count: `{report.get('mortgage_workflow_capture_queue_missing_har_count')}`",
        f"- mortgage_workflow_capture_queue_missing_har_properties: `{report.get('mortgage_workflow_capture_queue_missing_har_properties')}`",
        f"- mortgage_workflow_capture_queue_existing_har_needs_bodies_count: `{report.get('mortgage_workflow_capture_queue_existing_har_needs_bodies_count')}`",
        f"- mortgage_workflow_capture_queue_existing_har_needs_bodies_properties: `{report.get('mortgage_workflow_capture_queue_existing_har_needs_bodies_properties')}`",
        f"- mortgage_workflow_capture_queue_existing_har_needs_target_month_statement_pdf_count: `{report.get('mortgage_workflow_capture_queue_existing_har_needs_target_month_statement_pdf_count')}`",
        f"- mortgage_workflow_capture_queue_existing_har_needs_target_month_statement_pdf_properties: `{report.get('mortgage_workflow_capture_queue_existing_har_needs_target_month_statement_pdf_properties')}`",
        f"- mortgage_workflow_capture_queue_target_month_workflow_har_capture_required_count: `{report.get('mortgage_workflow_capture_queue_target_month_workflow_har_capture_required_count')}`",
        f"- mortgage_workflow_capture_queue_target_month_workflow_har_capture_required_properties: `{report.get('mortgage_workflow_capture_queue_target_month_workflow_har_capture_required_properties')}`",
        f"- mortgage_workflow_capture_queue_existing_workflow_har_paths: `{report.get('mortgage_workflow_capture_queue_existing_workflow_har_paths')}`",
        f"- mortgage_workflow_capture_queue_existing_workflow_har_path_sizes: `{report.get('mortgage_workflow_capture_queue_existing_workflow_har_path_sizes')}`",
        f"- mortgage_workflow_capture_queue_rejected_discovered_har_candidate_count: `{report.get('mortgage_workflow_capture_queue_rejected_discovered_har_candidate_count')}`",
        f"- mortgage_workflow_capture_queue_rejected_discovered_har_candidate_paths: `{report.get('mortgage_workflow_capture_queue_rejected_discovered_har_candidate_paths')}`",
        f"- mortgage_workflow_capture_queue_command_precondition_statuses: `{report.get('mortgage_workflow_capture_queue_command_precondition_statuses')}`",
        f"- mortgage_workflow_capture_queue_command_precondition_reasons: `{report.get('mortgage_workflow_capture_queue_command_precondition_reasons')}`",
        f"- mortgage_workflow_capture_queue_blocked_by_auth_properties: `{report.get('mortgage_workflow_capture_queue_blocked_by_auth_properties')}`",
        f"- mortgage_workflow_capture_queue_missing_workflow_har_properties: `{report.get('mortgage_workflow_capture_queue_missing_workflow_har_properties')}`",
        f"- mortgage_workflow_capture_queue_duplicate_target_statement_months: `{report.get('mortgage_workflow_capture_queue_duplicate_target_statement_months')}`",
        f"- mortgage_workflow_capture_queue_target_month_recapture_reasons: `{report.get('mortgage_workflow_capture_queue_target_month_recapture_reasons')}`",
        f"- mortgage_workflow_capture_queue_target_month_recapture_properties: `{report.get('mortgage_workflow_capture_queue_target_month_recapture_properties')}`",
        f"- mortgage_workflow_capture_queue_target_month_candidate_pdf_payload_statuses: `{report.get('mortgage_workflow_capture_queue_target_month_candidate_pdf_payload_statuses')}`",
        f"- mortgage_workflow_capture_queue_target_month_candidate_document_ids: `{report.get('mortgage_workflow_capture_queue_target_month_candidate_document_ids')}`",
        f"- mortgage_workflow_capture_queue_credential_lookup_statuses: `{report.get('mortgage_workflow_capture_queue_credential_lookup_statuses')}`",
        f"- mortgage_workflow_capture_queue_credential_lookup_failure_reasons: `{report.get('mortgage_workflow_capture_queue_credential_lookup_failure_reasons')}`",
        f"- mortgage_workflow_capture_queue_credential_lookup_item_names: `{report.get('mortgage_workflow_capture_queue_credential_lookup_item_names')}`",
        f"- mortgage_workflow_capture_queue_credential_lookup_expected_folder_names: `{report.get('mortgage_workflow_capture_queue_credential_lookup_expected_folder_names')}`",
        f"- mortgage_workflow_capture_queue_credential_lookup_uri_hosts: `{report.get('mortgage_workflow_capture_queue_credential_lookup_uri_hosts')}`",
        f"- mortgage_workflow_capture_queue_credential_login_hint_mismatch_properties: `{report.get('mortgage_workflow_capture_queue_credential_login_hint_mismatch_properties')}`",
        f"- mortgage_workflow_credential_repair_action_count: `{report.get('mortgage_workflow_credential_repair_action_count')}`",
        f"- mortgage_workflow_credential_repair_properties: `{report.get('mortgage_workflow_credential_repair_properties')}`",
        f"- mortgage_workflow_portal_auth_repair_action_count: `{report.get('mortgage_workflow_portal_auth_repair_action_count')}`",
        f"- mortgage_workflow_portal_auth_repair_properties: `{report.get('mortgage_workflow_portal_auth_repair_properties')}`",
        f"- mortgage_workflow_replayability_credential_repair_action_count: `{report.get('mortgage_workflow_replayability_credential_repair_action_count')}`",
        f"- mortgage_workflow_replayability_credential_repair_properties: `{report.get('mortgage_workflow_replayability_credential_repair_properties')}`",
        f"- mortgage_workflow_replayability_portal_auth_repair_action_count: `{report.get('mortgage_workflow_replayability_portal_auth_repair_action_count')}`",
        f"- mortgage_workflow_replayability_portal_auth_repair_properties: `{report.get('mortgage_workflow_replayability_portal_auth_repair_properties')}`",
        f"- mortgage_workflow_capture_queue_har_paths: `{report.get('mortgage_workflow_capture_queue_har_paths')}`",
        f"- mortgage_workflow_capture_queue_capture_commands: `{report.get('mortgage_workflow_capture_queue_capture_commands')}`",
        f"- mortgage_workflow_capture_queue_validation_commands: `{report.get('mortgage_workflow_capture_queue_validation_commands')}`",
        f"- mortgage_workflow_capture_queue_advance_commands: `{report.get('mortgage_workflow_capture_queue_advance_commands')}`",
        f"- mortgage_workflow_capture_queue_advance_apply_commands: `{report.get('mortgage_workflow_capture_queue_advance_apply_commands')}`",
        f"- mortgage_workflow_capture_queue_manual_capture_count: `{report.get('mortgage_workflow_capture_queue_manual_capture_count')}`",
        f"- mortgage_workflow_capture_queue_manual_capture_properties: `{report.get('mortgage_workflow_capture_queue_manual_capture_properties')}`",
        f"- mortgage_workflow_capture_queue_automatic_capture_count: `{report.get('mortgage_workflow_capture_queue_automatic_capture_count')}`",
        f"- mortgage_workflow_capture_queue_automatic_capture_properties: `{report.get('mortgage_workflow_capture_queue_automatic_capture_properties')}`",
        f"- mortgage_workflow_capture_queue_tokenomics_blocking_count: `{report.get('mortgage_workflow_capture_queue_tokenomics_blocking_count')}`",
        f"- mortgage_workflow_capture_queue_tokenomics_blocking_properties: `{report.get('mortgage_workflow_capture_queue_tokenomics_blocking_properties')}`",
        f"- mortgage_workflow_capture_queue_tokenomics_non_blocking_count: `{report.get('mortgage_workflow_capture_queue_tokenomics_non_blocking_count')}`",
        f"- mortgage_workflow_capture_queue_tokenomics_non_blocking_properties: `{report.get('mortgage_workflow_capture_queue_tokenomics_non_blocking_properties')}`",
        f"- mortgage_workflow_capture_queue_tokenomics_workbook_write_blocker_count: `{report.get('mortgage_workflow_capture_queue_tokenomics_workbook_write_blocker_count')}`",
        f"- mortgage_workflow_capture_queue_tokenomics_workbook_write_blocker_properties: `{report.get('mortgage_workflow_capture_queue_tokenomics_workbook_write_blocker_properties')}`",
        f"- mortgage_workflow_capture_queue_tokenomics_workbook_write_blocker_reasons: `{report.get('mortgage_workflow_capture_queue_tokenomics_workbook_write_blocker_reasons')}`",
        f"- mortgage_workflow_capture_queue_processor_rc: `{report.get('mortgage_workflow_capture_queue_processor_rc')}`",
        f"- mortgage_workflow_capture_queue_processor_report: `{report.get('mortgage_workflow_capture_queue_processor_report')}`",
        f"- mortgage_workflow_capture_queue_processor_markdown: `{report.get('mortgage_workflow_capture_queue_processor_markdown')}`",
        f"- mortgage_workflow_capture_queue_processor_status: `{report.get('mortgage_workflow_capture_queue_processor_status')}`",
        f"- mortgage_workflow_capture_queue_processor_reason: `{report.get('mortgage_workflow_capture_queue_processor_reason')}`",
        f"- mortgage_workflow_capture_queue_processor_command_precondition_statuses: `{report.get('mortgage_workflow_capture_queue_processor_command_precondition_statuses')}`",
        f"- mortgage_workflow_capture_queue_processor_command_precondition_reasons: `{report.get('mortgage_workflow_capture_queue_processor_command_precondition_reasons')}`",
        f"- mortgage_workflow_capture_queue_processor_blocked_by_auth_properties: `{report.get('mortgage_workflow_capture_queue_processor_blocked_by_auth_properties')}`",
        f"- mortgage_workflow_capture_queue_processor_missing_workflow_har_properties: `{report.get('mortgage_workflow_capture_queue_processor_missing_workflow_har_properties')}`",
        f"- mortgage_workflow_capture_queue_processor_target_month_recapture_reasons: `{report.get('mortgage_workflow_capture_queue_processor_target_month_recapture_reasons')}`",
        f"- mortgage_workflow_capture_queue_processor_target_month_recapture_properties: `{report.get('mortgage_workflow_capture_queue_processor_target_month_recapture_properties')}`",
        f"- mortgage_workflow_capture_queue_processor_target_month_candidate_pdf_payload_statuses: `{report.get('mortgage_workflow_capture_queue_processor_target_month_candidate_pdf_payload_statuses')}`",
        f"- mortgage_workflow_capture_queue_processor_target_month_candidate_document_ids: `{report.get('mortgage_workflow_capture_queue_processor_target_month_candidate_document_ids')}`",
        f"- mortgage_workflow_capture_queue_processor_credential_lookup_statuses: `{report.get('mortgage_workflow_capture_queue_processor_credential_lookup_statuses')}`",
        f"- mortgage_workflow_capture_queue_processor_credential_lookup_failure_reasons: `{report.get('mortgage_workflow_capture_queue_processor_credential_lookup_failure_reasons')}`",
        f"- mortgage_workflow_capture_queue_processor_credential_lookup_item_names: `{report.get('mortgage_workflow_capture_queue_processor_credential_lookup_item_names')}`",
        f"- mortgage_workflow_capture_queue_processor_credential_lookup_expected_folder_names: `{report.get('mortgage_workflow_capture_queue_processor_credential_lookup_expected_folder_names')}`",
        f"- mortgage_workflow_capture_queue_processor_credential_lookup_uri_hosts: `{report.get('mortgage_workflow_capture_queue_processor_credential_lookup_uri_hosts')}`",
        f"- mortgage_workflow_capture_queue_processor_credential_login_hint_mismatch_properties: `{report.get('mortgage_workflow_capture_queue_processor_credential_login_hint_mismatch_properties')}`",
        f"- mortgage_workflow_capture_queue_processor_credential_repair_action_count: `{report.get('mortgage_workflow_capture_queue_processor_credential_repair_action_count')}`",
        f"- mortgage_workflow_capture_queue_processor_credential_repair_properties: `{report.get('mortgage_workflow_capture_queue_processor_credential_repair_properties')}`",
        f"- mortgage_workflow_capture_queue_processor_portal_auth_repair_action_count: `{report.get('mortgage_workflow_capture_queue_processor_portal_auth_repair_action_count')}`",
        f"- mortgage_workflow_capture_queue_processor_portal_auth_repair_properties: `{report.get('mortgage_workflow_capture_queue_processor_portal_auth_repair_properties')}`",
        f"- mortgage_workflow_capture_queue_processor_apply: `{report.get('mortgage_workflow_capture_queue_processor_apply')}`",
        f"- mortgage_workflow_capture_queue_processor_process_existing_body_recaptures: `{report.get('mortgage_workflow_capture_queue_processor_process_existing_body_recaptures')}`",
        f"- mortgage_workflow_capture_queue_processor_enable_verified_registry_entry: `{report.get('mortgage_workflow_capture_queue_processor_enable_verified_registry_entry')}`",
        f"- mortgage_workflow_capture_queue_processor_processed_count: `{report.get('mortgage_workflow_capture_queue_processor_processed_count')}`",
        f"- mortgage_workflow_capture_queue_processor_applied_count: `{report.get('mortgage_workflow_capture_queue_processor_applied_count')}`",
        f"- mortgage_workflow_capture_queue_processor_dry_run_processed_count: `{report.get('mortgage_workflow_capture_queue_processor_dry_run_processed_count')}`",
        f"- mortgage_workflow_capture_queue_processor_pending_apply_count: `{report.get('mortgage_workflow_capture_queue_processor_pending_apply_count')}`",
        f"- mortgage_workflow_capture_queue_processor_pending_apply_properties: `{report.get('mortgage_workflow_capture_queue_processor_pending_apply_properties')}`",
        f"- mortgage_workflow_capture_queue_processor_durable_completion_count: `{report.get('mortgage_workflow_capture_queue_processor_durable_completion_count')}`",
        f"- mortgage_workflow_capture_queue_processor_durable_completion_properties: `{report.get('mortgage_workflow_capture_queue_processor_durable_completion_properties')}`",
        f"- mortgage_workflow_capture_queue_processor_safe_to_run_automatically: `{report.get('mortgage_workflow_capture_queue_processor_safe_to_run_automatically')}`",
        f"- mortgage_workflow_capture_queue_processor_missing_har_count: `{report.get('mortgage_workflow_capture_queue_processor_missing_har_count')}`",
        f"- mortgage_workflow_capture_queue_processor_missing_har_properties: `{report.get('mortgage_workflow_capture_queue_processor_missing_har_properties')}`",
        f"- mortgage_workflow_capture_queue_processor_invalid_har_count: `{report.get('mortgage_workflow_capture_queue_processor_invalid_har_count')}`",
        f"- mortgage_workflow_capture_queue_processor_invalid_har_properties: `{report.get('mortgage_workflow_capture_queue_processor_invalid_har_properties')}`",
        f"- mortgage_workflow_capture_queue_processor_auth_blocked_before_capture_count: `{report.get('mortgage_workflow_capture_queue_processor_auth_blocked_before_capture_count')}`",
        f"- mortgage_workflow_capture_queue_processor_auth_blocked_before_capture_properties: `{report.get('mortgage_workflow_capture_queue_processor_auth_blocked_before_capture_properties')}`",
        f"- mortgage_workflow_capture_queue_processor_existing_har_recapture_skipped_count: `{report.get('mortgage_workflow_capture_queue_processor_existing_har_recapture_skipped_count')}`",
        f"- mortgage_workflow_capture_queue_processor_existing_har_recapture_skipped_properties: `{report.get('mortgage_workflow_capture_queue_processor_existing_har_recapture_skipped_properties')}`",
        f"- mortgage_workflow_capture_queue_processor_existing_har_target_month_pdf_recapture_skipped_count: `{report.get('mortgage_workflow_capture_queue_processor_existing_har_target_month_pdf_recapture_skipped_count')}`",
        f"- mortgage_workflow_capture_queue_processor_existing_har_target_month_pdf_recapture_skipped_properties: `{report.get('mortgage_workflow_capture_queue_processor_existing_har_target_month_pdf_recapture_skipped_properties')}`",
        f"- mortgage_workflow_capture_queue_processor_existing_body_recapture_skipped_count: `{report.get('mortgage_workflow_capture_queue_processor_existing_body_recapture_skipped_count')}`",
        f"- mortgage_workflow_capture_queue_processor_existing_body_recapture_skipped_properties: `{report.get('mortgage_workflow_capture_queue_processor_existing_body_recapture_skipped_properties')}`",
        f"- mortgage_workflow_capture_queue_processor_queue_blocker_status: `{report.get('mortgage_workflow_capture_queue_processor_queue_blocker_status')}`",
        f"- mortgage_workflow_capture_queue_processor_queue_blocker_reason: `{report.get('mortgage_workflow_capture_queue_processor_queue_blocker_reason')}`",
        f"- mortgage_workflow_capture_queue_processor_queue_blocker_count: `{report.get('mortgage_workflow_capture_queue_processor_queue_blocker_count')}`",
        f"- mortgage_workflow_capture_queue_processor_queue_blocker_properties: `{report.get('mortgage_workflow_capture_queue_processor_queue_blocker_properties')}`",
        f"- mortgage_workflow_capture_queue_processor_operator_next_action_count: `{report.get('mortgage_workflow_capture_queue_processor_operator_next_action_count')}`",
        f"- mortgage_workflow_capture_queue_processor_operator_next_action_properties: `{report.get('mortgage_workflow_capture_queue_processor_operator_next_action_properties')}`",
        f"- mortgage_workflow_registry_handoff_count: `{report.get('mortgage_workflow_registry_handoff_count')}`",
        f"- mortgage_workflow_registry_handoff_paths: `{report.get('mortgage_workflow_registry_handoff_paths')}`",
        f"- mortgage_workflow_registry_safe_to_register_automatically_count: `{report.get('mortgage_workflow_registry_safe_to_register_automatically_count')}`",
        f"- mortgage_workflow_property_matrix_count: `{report.get('mortgage_workflow_property_matrix_count')}`",
        f"- mortgage_workflow_property_matrix_review_count: `{report.get('mortgage_workflow_property_matrix_review_count')}`",
        f"- mortgage_workflow_property_matrix_manual_action_count: `{report.get('mortgage_workflow_property_matrix_manual_action_count')}`",
        f"- mortgage_workflow_property_matrix_safe_action_count: `{report.get('mortgage_workflow_property_matrix_safe_action_count')}`",
        f"- mortgage_workflow_automation_blocked_with_current_statement_count: `{report.get('mortgage_workflow_automation_blocked_with_current_statement_count')}`",
        f"- mortgage_workflow_automation_blocked_with_current_statement_properties: `{report.get('mortgage_workflow_automation_blocked_with_current_statement_properties')}`",
        f"- mortgage_workflow_current_cycle_future_automation_attention_required: `{report.get('mortgage_workflow_current_cycle_future_automation_attention_required')}`",
        f"- mortgage_workflow_current_cycle_future_automation_attention_properties: `{report.get('mortgage_workflow_current_cycle_future_automation_attention_properties')}`",
        f"- mortgage_workflow_future_automation_repair_action_count: `{report.get('mortgage_workflow_future_automation_repair_action_count')}`",
        f"- mortgage_workflow_future_automation_repair_properties: `{report.get('mortgage_workflow_future_automation_repair_properties')}`",
        f"- mortgage_workflow_future_automation_repair_reasons: `{report.get('mortgage_workflow_future_automation_repair_reasons')}`",
        f"- mortgage_workflow_replayability_citadel_status: `{report.get('mortgage_workflow_replayability_citadel_status')}`",
        f"- mortgage_workflow_replayability_citadel_ready: `{report.get('mortgage_workflow_replayability_citadel_ready')}`",
        f"- mortgage_workflow_replayability_citadel_blocker: `{report.get('mortgage_workflow_replayability_citadel_blocker')}`",
        "",
        "## Scope",
        "",
        f"- mortgage_statement_target_month: `{report.get('mortgage_statement_target_month')}`",
        f"- mortgage_statement_target_month_kind: `{report.get('mortgage_statement_target_month_kind')}`",
        f"- mortgage_statement_target_month_basis: `{report.get('mortgage_statement_target_month_basis')}`",
        f"- mortgage_workflow_month_semantics: `{report.get('mortgage_workflow_month_semantics')}`",
        f"- downloader_profile: `{report.get('mortgage_downloader_profile')}`",
        f"- enabled_downloaders: `{report.get('mortgage_downloader_enabled_count')}`",
        f"- eligible_co_owner_paid_downloaders: `{report.get('mortgage_downloader_eligible_count')}`",
        f"- skipped_non_co_owner_paid_downloaders: `{report.get('mortgage_downloader_skipped_non_co_owner_paid_count')}`",
        f"- failed_downloaders: `{report.get('mortgage_downloader_failed_count')}`",
        f"- expected_target_months: `{report.get('mortgage_downloader_expected_target_months')}`",
        f"- effective_statement_target_month: `{report.get('mortgage_downloader_effective_statement_target_month')}`",
        f"- target_month_differs_from_expected_months: `{report.get('mortgage_downloader_target_month_differs_from_expected_months')}`",
        f"- target_month_statement_available_count: `{report.get('mortgage_downloader_target_month_statement_available_count')}`",
        f"- target_month_statement_gap_count: `{report.get('mortgage_downloader_target_month_statement_gap_count')}`",
        f"- target_month_statement_gap_properties: `{report.get('mortgage_downloader_target_month_statement_gap_properties')}`",
        f"- current_cycle_statement_ready: `{report.get('mortgage_downloader_current_cycle_statement_ready')}`",
        f"- current_cycle_statement_blocker_count: `{report.get('mortgage_downloader_current_cycle_statement_blocker_count')}`",
        f"- current_cycle_future_automation_attention_required: `{report.get('mortgage_downloader_current_cycle_future_automation_attention_required')}`",
        f"- failed_target_month_statement_available_count: `{report.get('mortgage_downloader_failed_target_month_statement_available_count')}`",
        f"- failed_target_month_statement_available_properties: `{report.get('mortgage_downloader_failed_target_month_statement_available_properties')}`",
        f"- safe_to_run_automatically: `{report.get('mortgage_downloader_safe_to_run_automatically')}`",
        f"- safe_downloader_count: `{report.get('mortgage_downloader_safe_downloader_count')}`",
        f"- unsafe_downloader_count: `{report.get('mortgage_downloader_unsafe_downloader_count')}`",
        f"- unsafe_downloader_properties: `{report.get('mortgage_downloader_unsafe_downloader_properties')}`",
        f"- automation_attention_count: `{report.get('mortgage_downloader_automation_attention_count')}`",
        f"- automation_attention_properties: `{report.get('mortgage_downloader_automation_attention_properties')}`",
        f"- automation_attention_reasons: `{report.get('mortgage_downloader_automation_attention_reasons')}`",
        f"- downloader_status_counts: `{report.get('mortgage_downloader_status_counts')}`",
        f"- downloader_summaries: `{report.get('mortgage_downloader_summaries')}`",
        "",
        "## Citadel / 90 Madison",
        "",
        f"- har_preflight_status: `{report.get('mortgage_downloader_citadel_har_preflight_status')}`",
        f"- har_preflight_capture_quality_status: `{report.get('mortgage_downloader_citadel_har_preflight_capture_quality_status')}`",
        f"- har_preflight_replay_blocker: `{report.get('mortgage_downloader_citadel_har_preflight_replay_blocker')}`",
        f"- har_preflight_missing_response_body_count: `{report.get('mortgage_downloader_citadel_har_preflight_missing_response_body_count')}`",
        f"- har_preflight_missing_response_body_path_counts: `{report.get('mortgage_downloader_citadel_har_preflight_missing_response_body_path_counts')}`",
        f"- har_preflight_response_body_requirements: `{report.get('mortgage_downloader_citadel_har_preflight_response_body_requirements')}`",
        f"- har_preflight_direct_pdf_response_count: `{report.get('mortgage_downloader_citadel_har_preflight_direct_pdf_response_count')}`",
        f"- har_preflight_source_direct_pdf_candidate_count: `{report.get('mortgage_downloader_citadel_har_preflight_source_direct_pdf_candidate_count')}`",
        f"- har_preflight_source_direct_pdf_path_counts: `{report.get('mortgage_downloader_citadel_har_preflight_source_direct_pdf_path_counts')}`",
        f"- har_preflight_source_direct_pdf_filenames: `{report.get('mortgage_downloader_citadel_har_preflight_source_direct_pdf_filenames')}`",
        f"- har_preflight_target_month_direct_pdf_filenames: `{report.get('mortgage_downloader_citadel_har_preflight_target_month_direct_pdf_filenames')}`",
        f"- har_preflight_target_month_direct_pdf_body_missing_candidate_count: `{report.get('mortgage_downloader_citadel_har_preflight_target_month_direct_pdf_body_missing_candidate_count')}`",
        f"- har_preflight_source_required_response_candidate_count: `{report.get('mortgage_downloader_citadel_har_preflight_source_required_response_candidate_count')}`",
        f"- har_preflight_source_required_response_path_counts: `{report.get('mortgage_downloader_citadel_har_preflight_source_required_response_path_counts')}`",
        f"- har_preflight_direct_pdf_missing_response_count: `{report.get('mortgage_downloader_citadel_har_preflight_direct_pdf_missing_response_count')}`",
        f"- har_preflight_can_replay_documents: `{report.get('mortgage_downloader_citadel_har_preflight_can_replay_documents')}`",
        f"- har_preflight_target_month: `{report.get('mortgage_downloader_citadel_har_preflight_target_month')}`",
        f"- har_preflight_target_month_replayable_document_available: `{report.get('mortgage_downloader_citadel_har_preflight_target_month_replayable_document_available')}`",
        f"- har_preflight_target_month_replayable_document_payload_count: `{report.get('mortgage_downloader_citadel_har_preflight_target_month_replayable_document_payload_count')}`",
        f"- har_preflight_replayable_statement_months: `{report.get('mortgage_downloader_citadel_har_preflight_replayable_statement_months')}`",
        f"- har_preflight_statement_document_months: `{report.get('mortgage_downloader_citadel_har_preflight_statement_document_months')}`",
        f"- har_preflight_next_action_status: `{report.get('mortgage_downloader_citadel_har_preflight_next_action_status')}`",
        f"- har_preflight_next_action_command: `{report.get('mortgage_downloader_citadel_har_preflight_next_action_command')}`",
        f"- har_preflight_next_action_target_month: `{report.get('mortgage_downloader_citadel_har_preflight_next_action_target_month')}`",
        f"- har_preflight_next_action_target_month_replayable_document_available: `{report.get('mortgage_downloader_citadel_har_preflight_next_action_target_month_replayable_document_available')}`",
        f"- har_preflight_next_action_target_month_replayable_document_payload_count: `{report.get('mortgage_downloader_citadel_har_preflight_next_action_target_month_replayable_document_payload_count')}`",
        f"- har_preflight_next_action_capture_command: `{report.get('mortgage_downloader_citadel_har_preflight_next_action_capture_command')}`",
        f"- har_preflight_next_action_required_response_paths: `{report.get('mortgage_downloader_citadel_har_preflight_next_action_required_response_paths')}`",
        f"- har_preflight_next_action_response_body_requirements: `{report.get('mortgage_downloader_citadel_har_preflight_next_action_response_body_requirements')}`",
        f"- har_preflight_next_action_source_direct_pdf_filenames: `{report.get('mortgage_downloader_citadel_har_preflight_next_action_source_direct_pdf_filenames')}`",
        f"- har_preflight_next_action_target_month_direct_pdf_filenames: `{report.get('mortgage_downloader_citadel_har_preflight_next_action_target_month_direct_pdf_filenames')}`",
        f"- har_preflight_next_action_target_month_direct_pdf_body_missing_candidate_count: `{report.get('mortgage_downloader_citadel_har_preflight_next_action_target_month_direct_pdf_body_missing_candidate_count')}`",
        f"- capture_plan_report: `{report.get('mortgage_downloader_citadel_capture_plan_report')}`",
        f"- capture_plan_markdown: `{report.get('mortgage_downloader_citadel_capture_plan_markdown')}`",
        f"- capture_plan_status: `{report.get('mortgage_downloader_citadel_capture_plan_status')}`",
        f"- capture_plan_latest_live_auth_report: `{report.get('mortgage_downloader_citadel_capture_plan_latest_live_auth_report')}`",
        f"- capture_plan_latest_live_auth_status: `{report.get('mortgage_downloader_citadel_capture_plan_latest_live_auth_status')}`",
        f"- capture_plan_latest_live_auth_blocker: `{report.get('mortgage_downloader_citadel_capture_plan_latest_live_auth_blocker')}`",
        f"- capture_plan_latest_live_auth_failure_reason: `{report.get('mortgage_downloader_citadel_capture_plan_latest_live_auth_auth_failure_reason')}`",
        f"- capture_plan_latest_live_auth_failure_visible_reason: `{report.get('mortgage_downloader_citadel_capture_plan_latest_live_auth_auth_failure_visible_reason')}`",
        f"- capture_plan_latest_live_auth_manual_auth_required: `{report.get('mortgage_downloader_citadel_capture_plan_latest_live_auth_manual_auth_required')}`",
        f"- capture_plan_latest_live_auth_manual_auth_file: `{report.get('mortgage_downloader_citadel_capture_plan_latest_live_auth_manual_auth_file')}`",
        f"- capture_plan_latest_live_auth_oauth_error_codes: `{report.get('mortgage_downloader_citadel_capture_plan_latest_live_auth_oauth_password_grant_error_codes')}`",
        f"- capture_plan_capture_precondition_status: `{report.get('mortgage_downloader_citadel_capture_plan_capture_precondition_status')}`",
        f"- capture_plan_capture_precondition_blocker: `{report.get('mortgage_downloader_citadel_capture_plan_capture_precondition_blocker')}`",
        f"- capture_plan_capture_precondition_reason: `{report.get('mortgage_downloader_citadel_capture_plan_capture_precondition_reason')}`",
        f"- capture_plan_capture_precondition_manual_auth_required: `{report.get('mortgage_downloader_citadel_capture_plan_capture_precondition_manual_auth_required')}`",
        f"- capture_plan_capture_precondition_next_action: `{report.get('mortgage_downloader_citadel_capture_plan_capture_precondition_next_action')}`",
        f"- capture_plan_capture_precondition_safe_to_capture_now: `{report.get('mortgage_downloader_citadel_capture_plan_capture_precondition_safe_to_capture_now')}`",
        f"- capture_plan_capture_precondition_prepare_command: `{report.get('mortgage_downloader_citadel_capture_plan_capture_precondition_prepare_command')}`",
        f"- capture_plan_capture_precondition_safe_to_prepare_now: `{report.get('mortgage_downloader_citadel_capture_plan_capture_precondition_safe_to_prepare_now')}`",
        f"- capture_plan_target_month: `{report.get('mortgage_downloader_citadel_capture_plan_target_month')}`",
        f"- capture_plan_capture_quality_status: `{report.get('mortgage_downloader_citadel_capture_plan_capture_quality_status')}`",
        f"- capture_plan_replay_blocker: `{report.get('mortgage_downloader_citadel_capture_plan_replay_blocker')}`",
        f"- capture_plan_missing_response_body_count: `{report.get('mortgage_downloader_citadel_capture_plan_missing_response_body_count')}`",
        f"- capture_plan_missing_response_body_path_counts: `{report.get('mortgage_downloader_citadel_capture_plan_missing_response_body_path_counts')}`",
        f"- capture_plan_target_month_replayable_document_available: `{report.get('mortgage_downloader_citadel_capture_plan_target_month_replayable_document_available')}`",
        f"- capture_plan_target_month_replayable_document_payload_count: `{report.get('mortgage_downloader_citadel_capture_plan_target_month_replayable_document_payload_count')}`",
        f"- capture_plan_replayable_statement_months: `{report.get('mortgage_downloader_citadel_capture_plan_replayable_statement_months')}`",
        f"- capture_plan_statement_document_months: `{report.get('mortgage_downloader_citadel_capture_plan_statement_document_months')}`",
        f"- capture_plan_capture_required: `{report.get('mortgage_downloader_citadel_capture_plan_capture_required')}`",
        f"- capture_plan_capture_command: `{report.get('mortgage_downloader_citadel_capture_plan_capture_command')}`",
        f"- capture_plan_next_command: `{report.get('mortgage_downloader_citadel_capture_plan_next_command')}`",
        f"- capture_plan_capture_har_path: `{report.get('mortgage_downloader_citadel_capture_plan_capture_har_path')}`",
        f"- capture_plan_canonical_har_path: `{report.get('mortgage_downloader_citadel_capture_plan_canonical_har_path')}`",
        f"- capture_plan_post_capture_diagnostics_command: `{report.get('mortgage_downloader_citadel_capture_plan_post_capture_diagnostics_command')}`",
        f"- capture_plan_post_capture_check_script_path: `{report.get('mortgage_downloader_citadel_capture_plan_post_capture_check_script_path')}`",
        f"- capture_plan_post_capture_check_command: `{report.get('mortgage_downloader_citadel_capture_plan_post_capture_check_command')}`",
        f"- capture_plan_post_capture_check_safe_to_run_after_har_capture: `{report.get('mortgage_downloader_citadel_capture_plan_post_capture_check_safe_to_run_after_har_capture')}`",
        f"- capture_plan_validate_existing_capture_command: `{report.get('mortgage_downloader_citadel_capture_plan_validate_existing_capture_command')}`",
        f"- capture_plan_advance_capture_workflow_evidence_dry_run_command: `{report.get('mortgage_downloader_citadel_capture_plan_advance_capture_workflow_evidence_dry_run_command')}`",
        f"- capture_plan_advance_capture_workflow_evidence_apply_command: `{report.get('mortgage_downloader_citadel_capture_plan_advance_capture_workflow_evidence_apply_command')}`",
        f"- capture_plan_install_verified_capture_report: `{report.get('mortgage_downloader_citadel_capture_plan_install_verified_capture_report')}`",
        f"- capture_plan_install_verified_capture_dry_run_command: `{report.get('mortgage_downloader_citadel_capture_plan_install_verified_capture_dry_run_command')}`",
        f"- capture_plan_install_verified_capture_apply_command: `{report.get('mortgage_downloader_citadel_capture_plan_install_verified_capture_apply_command')}`",
        f"- capture_plan_install_verified_capture_direct_dry_run_command: `{report.get('mortgage_downloader_citadel_capture_plan_install_verified_capture_direct_dry_run_command')}`",
        f"- capture_plan_install_verified_capture_direct_apply_command: `{report.get('mortgage_downloader_citadel_capture_plan_install_verified_capture_direct_apply_command')}`",
        f"- capture_plan_workflow_evidence_advance_report: `{report.get('mortgage_downloader_citadel_capture_plan_workflow_evidence_advance_report')}`",
        f"- capture_plan_advance_workflow_evidence_command: `{report.get('mortgage_downloader_citadel_capture_plan_advance_workflow_evidence_command')}`",
        f"- capture_plan_advance_workflow_evidence_apply_command: `{report.get('mortgage_downloader_citadel_capture_plan_advance_workflow_evidence_apply_command')}`",
        f"- capture_plan_required_response_path_counts: `{report.get('mortgage_downloader_citadel_capture_plan_required_response_path_counts')}`",
        f"- capture_plan_response_body_requirements: `{report.get('mortgage_downloader_citadel_capture_plan_response_body_requirements')}`",
        f"- capture_plan_source_direct_pdf_candidate_count: `{report.get('mortgage_downloader_citadel_capture_plan_source_direct_pdf_candidate_count')}`",
        f"- capture_plan_source_direct_pdf_path_counts: `{report.get('mortgage_downloader_citadel_capture_plan_source_direct_pdf_path_counts')}`",
        f"- capture_plan_source_direct_pdf_filenames: `{report.get('mortgage_downloader_citadel_capture_plan_source_direct_pdf_filenames')}`",
        f"- capture_plan_target_month_direct_pdf_filenames: `{report.get('mortgage_downloader_citadel_capture_plan_target_month_direct_pdf_filenames')}`",
        f"- capture_plan_target_month_direct_pdf_body_missing_candidate_count: `{report.get('mortgage_downloader_citadel_capture_plan_target_month_direct_pdf_body_missing_candidate_count')}`",
        f"- capture_plan_source_required_response_candidate_count: `{report.get('mortgage_downloader_citadel_capture_plan_source_required_response_candidate_count')}`",
        f"- capture_plan_source_required_response_path_counts: `{report.get('mortgage_downloader_citadel_capture_plan_source_required_response_path_counts')}`",
        f"- capture_plan_safe_to_run_automatically: `{report.get('mortgage_downloader_citadel_capture_plan_safe_to_run_automatically')}`",
        f"- capture_plan_raw_source_urls_included: `{report.get('mortgage_downloader_citadel_capture_plan_raw_source_urls_included')}`",
        f"- capture_plan_secrets_included: `{report.get('mortgage_downloader_citadel_capture_plan_secrets_included')}`",
        f"- prepare_status: `{report.get('mortgage_downloader_citadel_prepare_status')}`",
        f"- prepare_rc: `{report.get('mortgage_downloader_citadel_prepare_rc')}`",
        f"- prepare_reason: `{report.get('mortgage_downloader_citadel_prepare_reason')}`",
        f"- prepare_successful_password_request_found: `{report.get('mortgage_downloader_citadel_prepare_successful_password_request_found')}`",
        f"- prepare_bw_session_status: `{report.get('mortgage_downloader_citadel_prepare_bw_session_status')}`",
        f"- prepare_bw_item_found: `{report.get('mortgage_downloader_citadel_prepare_bw_item_found')}`",
        f"- prepare_bw_item_uri_host_match: `{report.get('mortgage_downloader_citadel_prepare_bw_item_uri_host_match')}`",
        f"- prepare_username_matches_har: `{report.get('mortgage_downloader_citadel_prepare_username_matches_har')}`",
        f"- prepare_password_matched_before_update: `{report.get('mortgage_downloader_citadel_prepare_password_matched_before_update')}`",
        f"- prepare_password_updated: `{report.get('mortgage_downloader_citadel_prepare_password_updated')}`",
        f"- auth_retryable_portal_failure: `{report.get('mortgage_downloader_citadel_auth_retryable_portal_failure')}`",
        f"- auth_retryable_reason: `{report.get('mortgage_downloader_citadel_auth_retryable_reason')}`",
        f"- capture_status: `{report.get('mortgage_downloader_citadel_capture_status')}`",
        f"- capture_reason: `{report.get('mortgage_downloader_citadel_capture_reason')}`",
        f"- capture_next_action_status: `{report.get('mortgage_downloader_citadel_capture_next_action_status')}`",
        f"- capture_next_action_command: `{report.get('mortgage_downloader_citadel_capture_next_action_command')}`",
        f"- capture_next_action_target_month: `{report.get('mortgage_downloader_citadel_capture_next_action_target_month')}`",
        f"- capture_next_action_target_month_replayable_document_available: `{report.get('mortgage_downloader_citadel_capture_next_action_target_month_replayable_document_available')}`",
        f"- capture_next_action_target_month_replayable_document_payload_count: `{report.get('mortgage_downloader_citadel_capture_next_action_target_month_replayable_document_payload_count')}`",
        f"- capture_authenticated_found: `{report.get('mortgage_downloader_citadel_capture_authenticated_found')}`",
        f"- capture_manual_auth_file: `{report.get('mortgage_downloader_citadel_capture_manual_auth_file')}`",
        f"- capture_manual_auth_portal_url: `{report.get('mortgage_downloader_citadel_capture_manual_auth_portal_url')}`",
        f"- capture_manual_auth_next_command: `{report.get('mortgage_downloader_citadel_capture_manual_auth_next_command')}`",
        f"- capture_manual_auth_install_verified_har_dry_run_command: `{report.get('mortgage_downloader_citadel_capture_manual_auth_install_verified_har_dry_run_command')}`",
        f"- capture_manual_auth_install_verified_har_apply_command: `{report.get('mortgage_downloader_citadel_capture_manual_auth_install_verified_har_apply_command')}`",
        f"- capture_login_tab_count: `{report.get('mortgage_downloader_citadel_capture_login_tab_count')}`",
        f"- capture_non_login_tab_count: `{report.get('mortgage_downloader_citadel_capture_non_login_tab_count')}`",
        f"- capture_source_har_path_exists: `{report.get('mortgage_downloader_citadel_capture_source_har_path_exists')}`",
        f"- capture_source_direct_pdf_candidate_count: `{report.get('mortgage_downloader_citadel_capture_source_direct_pdf_candidate_count')}`",
        f"- capture_source_direct_pdf_path_counts: `{report.get('mortgage_downloader_citadel_capture_source_direct_pdf_path_counts')}`",
        f"- capture_source_direct_pdf_fetched_count: `{report.get('mortgage_downloader_citadel_capture_source_direct_pdf_fetched_count')}`",
        f"- capture_source_direct_pdf_replayable_count: `{report.get('mortgage_downloader_citadel_capture_source_direct_pdf_replayable_count')}`",
        f"- capture_source_required_response_candidate_count: `{report.get('mortgage_downloader_citadel_capture_source_required_response_candidate_count')}`",
        f"- capture_source_required_response_path_counts: `{report.get('mortgage_downloader_citadel_capture_source_required_response_path_counts')}`",
        f"- capture_source_required_response_fetched_count: `{report.get('mortgage_downloader_citadel_capture_source_required_response_fetched_count')}`",
        f"- capture_source_required_response_replayable_count: `{report.get('mortgage_downloader_citadel_capture_source_required_response_replayable_count')}`",
        f"- capture_required_response_paths: `{report.get('mortgage_downloader_citadel_capture_required_response_paths')}`",
        f"- capture_required_response_path_counts: `{report.get('mortgage_downloader_citadel_capture_required_response_path_counts')}`",
        f"- capture_required_response_path_progress: `{report.get('mortgage_downloader_citadel_capture_required_response_path_progress')}`",
        f"- capture_captured_required_response_paths: `{report.get('mortgage_downloader_citadel_capture_captured_required_response_paths')}`",
        f"- capture_captured_required_response_path_counts: `{report.get('mortgage_downloader_citadel_capture_captured_required_response_path_counts')}`",
        f"- capture_missing_required_response_paths: `{report.get('mortgage_downloader_citadel_capture_missing_required_response_paths')}`",
        f"- capture_missing_required_response_path_counts: `{report.get('mortgage_downloader_citadel_capture_missing_required_response_path_counts')}`",
        f"- capture_replayable_document_payload_count: `{report.get('mortgage_downloader_citadel_capture_replayable_document_payload_count')}`",
        f"- capture_target_month: `{report.get('mortgage_downloader_citadel_capture_target_month')}`",
        f"- capture_target_month_replayable_document_available: `{report.get('mortgage_downloader_citadel_capture_target_month_replayable_document_available')}`",
        f"- capture_target_month_replayable_document_payload_count: `{report.get('mortgage_downloader_citadel_capture_target_month_replayable_document_payload_count')}`",
        f"- capture_replayable_statement_months: `{report.get('mortgage_downloader_citadel_capture_replayable_statement_months')}`",
        f"- capture_statement_document_months: `{report.get('mortgage_downloader_citadel_capture_statement_document_months')}`",
        f"- capture_direct_pdf_response_count: `{report.get('mortgage_downloader_citadel_capture_direct_pdf_response_count')}`",
        f"- report_status: `{report.get('mortgage_downloader_citadel_report_status')}`",
        f"- target_month: `{report.get('mortgage_downloader_citadel_target_month')}`",
        f"- target_month_statement_available: `{report.get('mortgage_downloader_citadel_target_month_statement_available')}`",
        f"- target_month_existing_count: `{report.get('mortgage_downloader_citadel_target_month_existing_count')}`",
        f"- target_month_downloaded_count: `{report.get('mortgage_downloader_citadel_target_month_downloaded_count')}`",
        f"- target_month_skipped_count: `{report.get('mortgage_downloader_citadel_target_month_skipped_count')}`",
        f"- auth_preflight_status: `{report.get('mortgage_downloader_citadel_auth_preflight_status')}`",
        f"- auth_preflight_reason: `{report.get('mortgage_downloader_citadel_auth_preflight_reason')}`",
        f"- auth_preflight_authenticated_found: `{report.get('mortgage_downloader_citadel_auth_preflight_authenticated_found')}`",
        f"- auth_preflight_candidate_count: `{report.get('mortgage_downloader_citadel_auth_preflight_candidate_count')}`",
        f"- auth_preflight_login_tab_count: `{report.get('mortgage_downloader_citadel_auth_preflight_login_tab_count')}`",
        f"- auth_preflight_non_login_tab_count: `{report.get('mortgage_downloader_citadel_auth_preflight_non_login_tab_count')}`",
        f"- auth_preflight_scanned_count: `{report.get('mortgage_downloader_citadel_auth_preflight_scanned_count')}`",
        f"- direct_auth_status: `{report.get('mortgage_downloader_citadel_direct_auth_status')}`",
        f"- direct_browser_fallback_attempted: `{report.get('mortgage_downloader_citadel_direct_browser_fallback_attempted')}`",
        f"- direct_browser_fallback_authenticated_found: `{report.get('mortgage_downloader_citadel_direct_browser_fallback_authenticated_found')}`",
        f"- tab_scan_candidate_count: `{report.get('mortgage_downloader_citadel_tab_scan_candidate_count')}`",
        f"- tab_scan_scanned_count: `{report.get('mortgage_downloader_citadel_tab_scan_scanned_count')}`",
        f"- tab_scan_skipped_count: `{report.get('mortgage_downloader_citadel_tab_scan_skipped_count')}`",
        f"- tab_scan_fetch_timeout_ms: `{report.get('mortgage_downloader_citadel_tab_scan_fetch_timeout_ms')}`",
        f"- tab_scan_target_id_requested: `{report.get('mortgage_downloader_citadel_tab_scan_target_id_requested')}`",
        f"- tab_scan_target_id_found: `{report.get('mortgage_downloader_citadel_tab_scan_target_id_found')}`",
        f"- tab_scan_direct_fallback_target_selected: `{report.get('mortgage_downloader_citadel_tab_scan_direct_fallback_target_selected')}`",
        f"- credential_item_name: `{report.get('mortgage_downloader_citadel_credential_item_name')}`",
        f"- credential_item_uri_hosts: `{report.get('mortgage_downloader_citadel_credential_item_uri_hosts')}`",
        f"- credential_item_portal_host_match: `{report.get('mortgage_downloader_citadel_credential_item_portal_host_match')}`",
        f"- credential_login_hint_configured: `{report.get('mortgage_downloader_citadel_credential_login_hint_configured')}`",
        f"- credential_username_matches_login_hint: `{report.get('mortgage_downloader_citadel_credential_username_matches_login_hint')}`",
        f"- credential_item_name_matches_login_hint: `{report.get('mortgage_downloader_citadel_credential_item_name_matches_login_hint')}`",
        f"- credential_field_matches_login_hint: `{report.get('mortgage_downloader_citadel_credential_field_matches_login_hint')}`",
        f"- auth_failure_reason: `{report.get('mortgage_downloader_citadel_auth_failure_reason')}`",
        f"- auth_failure_visible_reason: `{report.get('mortgage_downloader_citadel_auth_failure_visible_reason')}`",
        f"- auth_visible_error: `{report.get('mortgage_downloader_citadel_auth_visible_error')}`",
        f"- credential_state_drift_suspected: `{report.get('mortgage_downloader_citadel_credential_state_drift_suspected')}`",
        f"- no_mfa_handoff_after_password: `{report.get('mortgage_downloader_citadel_direct_no_mfa_handoff_after_password')}`",
        f"- no_mfa_handoff_reason: `{report.get('mortgage_downloader_citadel_direct_no_mfa_handoff_reason')}`",
        f"- otp_required: `{report.get('mortgage_downloader_citadel_otp_required')}`",
        f"- otp_file: `{report.get('mortgage_downloader_citadel_otp_file')}`",
        f"- otp_next_command: `{report.get('mortgage_downloader_citadel_otp_next_command')}`",
        f"- manual_auth_required: `{report.get('mortgage_downloader_citadel_manual_auth_required')}`",
        f"- manual_auth_reason: `{report.get('mortgage_downloader_citadel_manual_auth_reason')}`",
        f"- portal_url: `{report.get('mortgage_downloader_citadel_manual_auth_portal_url')}`",
        f"- fresh_recaptcha_available: `{report.get('mortgage_downloader_citadel_direct_fresh_recaptcha_token_available')}`",
        f"- fresh_recaptcha_stage: `{report.get('mortgage_downloader_citadel_direct_recaptcha_eval_stage')}`",
        f"- fresh_recaptcha_error: `{report.get('mortgage_downloader_citadel_direct_recaptcha_eval_error')}`",
        f"- direct_error_codes: `{report.get('mortgage_downloader_citadel_direct_error_codes')}`",
        f"- har_auth_diagnostics_enabled: `{report.get('mortgage_downloader_citadel_har_auth_diagnostics_enabled')}`",
        f"- har_auth_diagnostics_skipped_reason: `{report.get('mortgage_downloader_citadel_har_auth_diagnostics_skipped_reason')}`",
        f"- har_capture_quality_status: `{report.get('mortgage_downloader_citadel_har_workflow_capture_quality_status')}`",
        f"- har_replay_blocker: `{report.get('mortgage_downloader_citadel_har_workflow_replay_blocker')}`",
        f"- har_target_month: `{report.get('mortgage_downloader_citadel_har_workflow_target_month')}`",
        f"- har_target_month_replayable_document_available: `{report.get('mortgage_downloader_citadel_har_workflow_target_month_replayable_document_available')}`",
        f"- har_target_month_replayable_document_payload_count: `{report.get('mortgage_downloader_citadel_har_workflow_target_month_replayable_document_payload_count')}`",
        f"- har_replayable_statement_months: `{report.get('mortgage_downloader_citadel_har_workflow_replayable_statement_months')}`",
        f"- har_statement_document_months: `{report.get('mortgage_downloader_citadel_har_workflow_statement_document_months')}`",
        f"- har_missing_response_body_count: `{report.get('mortgage_downloader_citadel_har_workflow_missing_response_body_count')}`",
        f"- har_response_body_requirements: `{report.get('mortgage_downloader_citadel_har_workflow_response_body_requirements')}`",
        f"- capture_response_body_requirements: `{report.get('mortgage_downloader_citadel_capture_response_body_requirements')}`",
        f"- capture_response_body_requirement_role_capture_counts: `{report.get('mortgage_downloader_citadel_capture_response_body_requirement_role_capture_counts')}`",
        f"- capture_missing_response_body_requirements: `{report.get('mortgage_downloader_citadel_capture_missing_response_body_requirements')}`",
        f"- har_direct_pdf_response_count: `{report.get('mortgage_downloader_citadel_har_workflow_direct_pdf_response_count')}`",
        f"- har_source_direct_pdf_candidate_count: `{report.get('mortgage_downloader_citadel_har_workflow_source_direct_pdf_candidate_count')}`",
        f"- har_source_direct_pdf_path_counts: `{report.get('mortgage_downloader_citadel_har_workflow_source_direct_pdf_path_counts')}`",
        f"- har_source_direct_pdf_filenames: `{report.get('mortgage_downloader_citadel_har_workflow_source_direct_pdf_filenames')}`",
        f"- har_target_month_direct_pdf_filenames: `{report.get('mortgage_downloader_citadel_har_workflow_target_month_direct_pdf_filenames')}`",
        f"- har_target_month_direct_pdf_body_missing_candidate_count: `{report.get('mortgage_downloader_citadel_har_workflow_target_month_direct_pdf_body_missing_candidate_count')}`",
        f"- har_direct_pdf_missing_response_count: `{report.get('mortgage_downloader_citadel_har_workflow_direct_pdf_missing_response_count')}`",
        f"- har_workflow_can_replay_documents: `{report.get('mortgage_downloader_citadel_har_workflow_can_replay_documents')}`",
        f"- har_workflow_next_action_status: `{report.get('mortgage_downloader_citadel_har_workflow_next_action_status')}`",
        f"- har_workflow_next_action_command: `{report.get('mortgage_downloader_citadel_har_workflow_next_action_command')}`",
        f"- har_workflow_next_action_target_month: `{report.get('mortgage_downloader_citadel_har_workflow_next_action_target_month')}`",
        f"- har_workflow_next_action_target_month_replayable_document_available: `{report.get('mortgage_downloader_citadel_har_workflow_next_action_target_month_replayable_document_available')}`",
        f"- har_workflow_next_action_target_month_replayable_document_payload_count: `{report.get('mortgage_downloader_citadel_har_workflow_next_action_target_month_replayable_document_payload_count')}`",
        f"- har_workflow_next_action_replayable_statement_months: `{report.get('mortgage_downloader_citadel_har_workflow_next_action_replayable_statement_months')}`",
        f"- har_workflow_next_action_statement_document_months: `{report.get('mortgage_downloader_citadel_har_workflow_next_action_statement_document_months')}`",
        f"- har_workflow_next_action_source_direct_pdf_filenames: `{report.get('mortgage_downloader_citadel_har_workflow_next_action_source_direct_pdf_filenames')}`",
        f"- har_workflow_next_action_target_month_direct_pdf_filenames: `{report.get('mortgage_downloader_citadel_har_workflow_next_action_target_month_direct_pdf_filenames')}`",
        f"- har_workflow_next_action_target_month_direct_pdf_body_missing_candidate_count: `{report.get('mortgage_downloader_citadel_har_workflow_next_action_target_month_direct_pdf_body_missing_candidate_count')}`",
        f"- har_workflow_next_action_install_verified_capture_report: `{report.get('mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_report')}`",
        f"- har_workflow_next_action_install_verified_capture_apply_command: `{report.get('mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_apply_command')}`",
        f"- har_workflow_next_action_install_verified_capture_direct_apply_command: `{report.get('mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_direct_apply_command')}`",
        "",
    ]
    downloader_summaries = report.get("mortgage_downloader_summaries")
    if isinstance(downloader_summaries, list) and downloader_summaries:
        lines.extend([
            "## Downloader Statement Freshness",
            "",
        ])
        for index, item in enumerate(downloader_summaries, start=1):
            line = downloader_statement_freshness_line(index, item)
            if line:
                lines.append(line)
        lines.append("")
    property_matrix = report.get("mortgage_workflow_property_matrix")
    if isinstance(property_matrix, list) and property_matrix:
        lines.extend([
            "## Mortgage Property Matrix",
            "",
        ])
        for index, item in enumerate(property_matrix, start=1):
            line = property_matrix_line(index, item)
            if line:
                lines.append(line)
        lines.append("")
    tokenomics_capture_queue = report.get("coownership_tokenomics_blocking_capture_queue")
    if isinstance(tokenomics_capture_queue, list) and tokenomics_capture_queue:
        lines.extend([
            "## Co-Ownership Tokenomics Blocking Capture Queue",
            "",
        ])
        for index, item in enumerate(tokenomics_capture_queue, start=1):
            line = tokenomics_blocking_capture_line(index, item)
            if line:
                lines.append(line)
        lines.append("")
    workbook_blockers = report.get("coownership_tokenomics_workbook_write_blockers")
    if isinstance(workbook_blockers, list) and workbook_blockers:
        lines.extend([
            "## Co-Ownership Tokenomics Workbook Write Blockers",
            "",
        ])
        for index, item in enumerate(workbook_blockers, start=1):
            line = workbook_blocker_line(index, item)
            if line:
                lines.append(line)
        lines.append("")
    tokenomics_records = report.get("tokenomics_co_owner_paid_statement_records")
    if isinstance(tokenomics_records, list) and tokenomics_records:
        lines.extend([
            "## Mortgage Tokenomics Statement Records",
            "",
        ])
        for index, item in enumerate(tokenomics_records, start=1):
            line = tokenomics_statement_line(index, item)
            if line:
                lines.append(line)
        lines.append("")
    lines.extend([
        "## Next Actions",
        "",
    ])
    action_added = False
    if report.get("mortgage_downloader_citadel_auth_retryable_portal_failure"):
        lines.extend([
            "- Retry Citadel later; the Bitwarden credential matched a successful HAR login, but the live portal rejected before MFA.",
            "- Do not update the Citadel Bitwarden password unless a later HAR or manual login proves it changed.",
        ])
        action_added = True
    elif report.get("mortgage_downloader_citadel_credential_state_drift_suspected"):
        lines.extend([
            "- Verify the Citadel/LoanSphere login in the browser and update the Bitwarden item if the portal account state changed.",
            "- Re-run `scripts/baselane_monthly_mortgage_workflow_idempotent.sh` after the login is corrected.",
        ])
        action_added = True
    elif report.get("mortgage_downloader_citadel_manual_auth_required"):
        lines.extend([
            "- Complete Citadel portal MFA in a CDP-visible browser tab.",
            "- Run `scripts/citadel_manual_auth_download.sh` to let the downloader reuse the authenticated session.",
        ])
        action_added = True
    elif report.get("mortgage_downloader_citadel_otp_required"):
        lines.extend([
            f"- Put the Citadel email OTP into `{report.get('mortgage_downloader_citadel_otp_file')}`.",
            f"- Re-run `{report.get('mortgage_downloader_citadel_otp_next_command')}`.",
        ])
        action_added = True
    if report.get("mortgage_downloader_citadel_target_month_statement_available") is False:
        lines.append(
            "- Citadel did not produce the configured target-month statement; rerun after the statement posts or capture a HAR that includes that month."
        )
        action_added = True
    if report.get("mortgage_downloader_target_month_statement_gap_count"):
        lines.append(
            "- Refresh target-month mortgage statements or downloader logic for properties in mortgage_downloader_target_month_statement_gap_properties."
        )
        action_added = True
    if report.get("mortgage_downloader_automation_attention_count"):
        lines.append(
            "- Review mortgage_downloader_automation_attention_details before treating the downloader as fully replayable."
        )
        action_added = True
    if report.get("mortgage_splits_rc"):
        lines.append("- Review the Baselane mortgage split diagnostic JSON for unsplit Citadel principal/interest rows.")
        action_added = True
    if report.get("no_mortgage_financials_guard_remaining_nonzero_count"):
        lines.append("- Review the no-DAO mortgage financials guard report before publishing monthly financials; co-owner-paid mortgage P&I rows remain nonzero.")
        action_added = True
    if report.get("no_mortgage_financials_guard_read_error_count"):
        lines.append("- Review the no-DAO mortgage financials guard read errors; at least one scoped financial artifact could not be inspected.")
        action_added = True
    if report.get("tokenomics_rc"):
        lines.append("- Review the Co-Ownership Mortgage Tokenomics workbook update report and rerun after fixing statement parsing or workbook access.")
        action_added = True
    if report.get("tokenomics_current_month_statement_gap_count"):
        lines.append("- Refresh current-month mortgage statements before rerunning tokenomics for properties in tokenomics_current_month_statement_gap_properties.")
        action_added = True
    if report.get("coownership_tokenomics_workbook_write_blocker_count"):
        lines.append("- Do not live-write the co-ownership tokenomics workbook until coownership_tokenomics_workbook_write_blockers is empty.")
        action_added = True
    if report.get("coownership_tokenomics_blocking_capture_queue_count"):
        lines.append("- Use coownership_tokenomics_blocking_capture_queue for the short capture/check list that blocks the tokenomics workbook write.")
        action_added = True
    if report.get("mortgage_downloader_coverage_missing_downloader_count"):
        lines.append("- Add downloader registry entries for the co-owner-paid properties listed in mortgage_downloader_coverage_missing_downloader_properties.")
        if report.get("mortgage_downloader_coverage_next_action_plan_count"):
            lines.append("- Use mortgage_downloader_coverage_next_action_plan for the per-property workflow-evidence checklist.")
        if report.get("mortgage_downloader_coverage_workflow_evidence_capture_plan_count"):
            lines.append("- Use mortgage_downloader_coverage_workflow_evidence_capture_plan for suggested HAR filenames, handoff commands, and offline HAR analysis commands.")
        action_added = True
    if report.get("mortgage_downloader_coverage_current_month_statement_gap_count"):
        lines.append("- Treat properties in mortgage_downloader_coverage_current_month_statement_gap_properties as not current-month-safe for tokenomics until refreshed statements are captured.")
        action_added = True
    if report.get("tokenomics_current_month_statement_gap_remediation_count"):
        lines.append("- Use tokenomics_current_month_statement_gap_remediation_plan before live-writing the co-ownership tokenomics workbook.")
        action_added = True
    if report.get("mortgage_downloader_coverage_blocked_configured_downloader_count"):
        lines.append("- Fix configured mortgage downloaders listed in mortgage_downloader_coverage_blocked_configured_downloaders before treating coverage as operational.")
        if report.get("mortgage_downloader_coverage_next_action_plan_count"):
            lines.append("- Use mortgage_downloader_coverage_next_action_plan for the exact capture command and required response paths.")
        action_added = True
    if report.get("mortgage_downloader_coverage_automation_repair_not_statement_gap_count"):
        lines.append("- Downloader automation still needs credential/auth repair for mortgage_downloader_coverage_automation_repair_not_statement_gap_properties, but current-month statement files satisfy tokenomics coverage.")
        action_added = True
    if report.get("mortgage_workflow_automation_blocked_with_current_statement_count"):
        lines.append("- Current-month statement files exist for mortgage_workflow_automation_blocked_with_current_statement_properties, but automation is still blocked; do not treat file availability as downloader health.")
        action_added = True
    if report.get("mortgage_workflow_replayability_status") == "review":
        lines.append("- Use mortgage_workflow_replayability_report for the consolidated offline replayability blockers before building additional downloader code.")
        action_added = True
    if report.get("mortgage_workflow_capture_queue_capture_count"):
        lines.append("- Use mortgage_workflow_capture_queue_markdown for the idempotent per-property visible-browser HAR capture queue.")
        action_added = True
    if report.get("mortgage_workflow_capture_queue_processor_processed_count"):
        lines.append("- Use mortgage_workflow_capture_queue_processor_report for idempotent post-capture HAR processing results.")
        action_added = True
    credential_repair_actions = report.get("mortgage_workflow_credential_repair_actions")
    if isinstance(credential_repair_actions, list) and credential_repair_actions:
        lines.extend([
            "",
            "### Mortgage Credential Repair Plan",
            "",
        ])
        for index, item in enumerate(credential_repair_actions, start=1):
            line = credential_repair_line(index, item)
            if line:
                lines.append(line)
        action_added = True
    portal_auth_repair_actions = report.get("mortgage_workflow_portal_auth_repair_actions")
    if isinstance(portal_auth_repair_actions, list) and portal_auth_repair_actions:
        lines.extend([
            "",
            "### Mortgage Portal Auth Repair Plan",
            "",
        ])
        for index, item in enumerate(portal_auth_repair_actions, start=1):
            line = portal_auth_repair_line(index, item)
            if line:
                lines.append(line)
        action_added = True
    replayability_credential_repair_actions = report.get(
        "mortgage_workflow_replayability_credential_repair_actions"
    )
    if isinstance(replayability_credential_repair_actions, list) and replayability_credential_repair_actions:
        lines.extend([
            "",
            "### Mortgage Replayability Credential Repair Plan",
            "",
        ])
        for index, item in enumerate(replayability_credential_repair_actions, start=1):
            line = credential_repair_line(index, item)
            if line:
                lines.append(line)
        action_added = True
    replayability_portal_auth_repair_actions = report.get(
        "mortgage_workflow_replayability_portal_auth_repair_actions"
    )
    if isinstance(replayability_portal_auth_repair_actions, list) and replayability_portal_auth_repair_actions:
        lines.extend([
            "",
            "### Mortgage Replayability Portal Auth Repair Plan",
            "",
        ])
        for index, item in enumerate(replayability_portal_auth_repair_actions, start=1):
            line = portal_auth_repair_line(index, item)
            if line:
                lines.append(line)
        action_added = True
    capture_queue_actions = report.get("mortgage_workflow_capture_queue_actions")
    if isinstance(capture_queue_actions, list) and capture_queue_actions:
        lines.extend([
            "",
            "### Mortgage Capture Queue Actions",
            "",
        ])
        for index, item in enumerate(capture_queue_actions, start=1):
            line = coverage_action_line(index, item)
            if line:
                lines.append(line)
        action_added = True
    registry_handoffs = report.get("mortgage_workflow_registry_handoffs")
    if isinstance(registry_handoffs, list) and registry_handoffs:
        lines.extend([
            "",
            "### Mortgage Registry Handoffs",
            "",
        ])
        for index, item in enumerate(registry_handoffs, start=1):
            line = coverage_action_line(index, item)
            if line:
                lines.append(line)
        action_added = True
    capture_queue_entries = report.get("mortgage_workflow_capture_queue_entries")
    if isinstance(capture_queue_entries, list) and capture_queue_entries:
        lines.extend([
            "",
            "### Mortgage Capture Queue",
            "",
        ])
        for index, item in enumerate(capture_queue_entries, start=1):
            line = coverage_action_line(index, item)
            if line:
                lines.append(line)
        action_added = True
    evidence_refresh_plan = report.get("mortgage_workflow_evidence_refresh_next_action_plan")
    if isinstance(evidence_refresh_plan, list) and evidence_refresh_plan:
        lines.extend([
            "",
            "### Mortgage Evidence Refresh Plan",
            "",
        ])
        for index, item in enumerate(evidence_refresh_plan, start=1):
            line = coverage_action_line(index, item)
            if line:
                lines.append(line)
        action_added = True
    evidence_advance_results = report.get("mortgage_workflow_evidence_advance_results")
    if isinstance(evidence_advance_results, list) and evidence_advance_results:
        lines.extend([
            "",
            "### Mortgage Evidence Advance Results",
            "",
        ])
        for index, item in enumerate(evidence_advance_results, start=1):
            line = coverage_action_line(index, item)
            if line:
                lines.append(line)
        action_added = True
    processor_results = report.get("mortgage_workflow_capture_queue_processor_results")
    if isinstance(processor_results, list) and processor_results:
        lines.extend([
            "",
            "### Mortgage Capture Queue Processor Results",
            "",
        ])
        for index, item in enumerate(processor_results, start=1):
            line = coverage_action_line(index, item)
            if line:
                lines.append(line)
        action_added = True
    processor_recapture_requirements = report.get(
        "mortgage_workflow_capture_queue_processor_existing_har_recapture_requirements"
    )
    processor_target_pdf_recapture_requirements = report.get(
        "mortgage_workflow_capture_queue_processor_existing_har_target_month_pdf_recapture_requirements"
    )
    processor_next_actions = report.get("mortgage_workflow_capture_queue_processor_operator_next_actions")
    recapture_lines = processor_action_lines(
        "Mortgage Capture Queue Processor Body Recapture Requirements",
        processor_recapture_requirements,
    )
    if recapture_lines:
        lines.extend([""])
        lines.extend(recapture_lines)
        action_added = True
    target_pdf_recapture_lines = processor_action_lines(
        "Mortgage Capture Queue Processor Target-Month PDF Recapture Requirements",
        processor_target_pdf_recapture_requirements,
    )
    if target_pdf_recapture_lines:
        lines.extend([""])
        lines.extend(target_pdf_recapture_lines)
        action_added = True
    next_action_lines = processor_action_lines(
        "Mortgage Capture Queue Processor Next Actions",
        processor_next_actions,
    )
    if next_action_lines:
        lines.extend([""])
        lines.extend(next_action_lines)
        action_added = True
    coverage_plan = report.get("mortgage_downloader_coverage_next_action_plan")
    if isinstance(coverage_plan, list) and coverage_plan:
        lines.extend([
            "",
            "### Mortgage Downloader Coverage Plan",
            "",
        ])
        for index, item in enumerate(coverage_plan, start=1):
            line = coverage_action_line(index, item)
            if line:
                lines.append(line)
        action_added = True
    blocked_downloaders = report.get("mortgage_downloader_coverage_blocked_configured_downloaders")
    if isinstance(blocked_downloaders, list) and blocked_downloaders:
        lines.extend([
            "",
            "### Blocked Configured Mortgage Downloaders",
            "",
        ])
        for index, item in enumerate(blocked_downloaders, start=1):
            line = blocked_downloader_line(index, item)
            if line:
                lines.append(line)
        action_added = True
    repair_not_gap_downloaders = report.get("mortgage_downloader_coverage_automation_repair_not_statement_gap_downloaders")
    if isinstance(repair_not_gap_downloaders, list) and repair_not_gap_downloaders:
        lines.extend([
            "",
            "### Downloader Automation Repair Not Blocking Tokenomics",
            "",
        ])
        for index, item in enumerate(repair_not_gap_downloaders, start=1):
            line = blocked_downloader_line(index, item)
            if line:
                lines.append(line)
        action_added = True
    tokenomics_gap_plan = report.get("tokenomics_current_month_statement_gap_remediation_plan")
    if isinstance(tokenomics_gap_plan, list) and tokenomics_gap_plan:
        lines.extend([
            "",
            "### Tokenomics Statement Gap Remediation Plan",
            "",
        ])
        for index, item in enumerate(tokenomics_gap_plan, start=1):
            line = tokenomics_gap_remediation_line(index, item)
            if line:
                lines.append(line)
        action_added = True
    replayability_plan = report.get("mortgage_workflow_replayability_next_action_plan")
    if isinstance(replayability_plan, list) and replayability_plan:
        lines.extend([
            "",
            "### Mortgage Replayability Plan",
            "",
        ])
        for index, item in enumerate(replayability_plan, start=1):
            line = coverage_action_line(index, item)
            if line:
                lines.append(line)
        action_added = True
    elif report.get("status") == "ok":
        lines.append("- No mortgage workflow review action is required.")
    elif not action_added:
        lines.append("- Review the gate JSON and downloader report for the current failure class.")
    lines.extend([
        "",
        "## Artifacts",
        "",
        f"- gate_report: `{os.environ.get('BASELANE_MORTGAGE_GATE_REPORT')}`",
        f"- downloader_report: `{report.get('mortgage_downloader_report')}`",
        f"- downloader_coverage_report: `{report.get('mortgage_downloader_coverage_report')}`",
        f"- evidence_advance_report: `{report.get('mortgage_workflow_evidence_advance_report')}`",
        f"- evidence_refresh_report: `{report.get('mortgage_workflow_evidence_refresh_report')}`",
        f"- capture_queue_processor_report: `{report.get('mortgage_workflow_capture_queue_processor_report')}`",
        f"- capture_queue_processor_markdown: `{report.get('mortgage_workflow_capture_queue_processor_markdown')}`",
        f"- replayability_report: `{report.get('mortgage_workflow_replayability_report')}`",
        f"- auth_preflight_report: `{report.get('mortgage_downloader_citadel_auth_preflight_report')}`",
        f"- manual_auth_file: `{report.get('mortgage_downloader_citadel_manual_auth_file')}`",
        f"- repair_packet: `{report.get('mortgage_workflow_repair_packet')}`",
        f"- repair_packet_markdown: `{report.get('mortgage_workflow_repair_packet_markdown')}`",
    ])
    write_text_if_changed(packet_path, "\n".join(lines).rstrip() + "\n")
gate_report_path = os.environ["BASELANE_MORTGAGE_GATE_REPORT"]
report["idempotency_digest"] = stable_report_digest(report)
gate_report_text = json.dumps(report, indent=2, sort_keys=True) + "\n"
write_text_if_changed(gate_report_path, gate_report_text)
PY
}

LAST=""
if [ -f "$STATE_FILE" ]; then
  LAST="$(cat "$STATE_FILE" 2>/dev/null || true)"
fi

DOWNLOADER_RC=0
CITADEL_RC=0
CITADEL_PREFLIGHT_RC=0
CITADEL_HAR_DIAGNOSTICS_RC=0
SPLITS_RC=0
TOKENOMICS_RC=0
COVERAGE_RC=0
MORTGAGE_EVIDENCE_ADVANCE_RC=0
MORTGAGE_EVIDENCE_REFRESH_RC=0
MORTGAGE_REPLAYABILITY_RC=0
MORTGAGE_CAPTURE_QUEUE_RC=0
MORTGAGE_CAPTURE_QUEUE_PROCESSOR_RC=0
MORTGAGE_ARCHIVE_RESTORE_RC=0
DOWNLOADER_AUTO_PROFILE=""
DOWNLOADER_ALLOW_HAR_MODE=0
DOWNLOADER_FALLBACK_ON_PREPARE_FAIL=0
TOKENOMICS_WRITE_GUARD_STATUS="not_evaluated"
TOKENOMICS_WRITE_GUARD_REASON=""

run_evidence_advance() {
  if [ -x "$MORTGAGE_EVIDENCE_ADVANCE_SCRIPT" ]; then
    echo "[mortgage-workflow] Advancing captured mortgage workflow evidence offline..."
    local advance_args=("--report" "$MORTGAGE_EVIDENCE_ADVANCE_REPORT" "--report-dir" "$REPORT_DIR" "--workspace-root" "$ROOT" "--coverage-report" "$MORTGAGE_COVERAGE_REPORT")
    if [ "${MORTGAGE_WORKFLOW_ADVANCE_WRITE_STUBS:-1}" = "1" ]; then
      advance_args+=("--write-stubs")
    fi
    if [ "${MORTGAGE_WORKFLOW_ADVANCE_INSTALL_REGISTRY_ENTRIES:-1}" = "1" ]; then
      advance_args+=("--install-registry-entries")
    fi
    if [ "${MORTGAGE_WORKFLOW_ADVANCE_APPLY_EVIDENCE:-0}" = "1" ]; then
      advance_args+=("--apply-evidence")
    fi
    if [ "${MORTGAGE_WORKFLOW_ADVANCE_APPLY_REGISTRY:-0}" = "1" ]; then
      advance_args+=("--apply-registry")
    fi
    if [ "${MORTGAGE_WORKFLOW_ADVANCE_ENABLE_VERIFIED_REGISTRY_ENTRY:-0}" = "1" ]; then
      advance_args+=("--enable-verified-registry-entry")
    fi
    set +e
    MORTGAGE_STATEMENT_TARGET_MONTH="$MORTGAGE_STATEMENT_TARGET_MONTH" \
    BASELANE_MORTGAGE_STATEMENT_TARGET_MONTH="$MORTGAGE_STATEMENT_TARGET_MONTH" \
    MORTGAGE_WORKFLOW_TARGET_MONTH="$MORTGAGE_STATEMENT_TARGET_MONTH" \
    CITADEL_STATEMENT_TARGET_MONTH="$DOWNLOADER_CITADEL_TARGET_MONTH" \
    "${MORTGAGE_WORKFLOW_STEP_TIMEOUT_ARGS[@]}" "$PY" "$MORTGAGE_EVIDENCE_ADVANCE_SCRIPT" "${advance_args[@]}" > "$MORTGAGE_EVIDENCE_ADVANCE_REPORT.stdout" 2>"$MORTGAGE_EVIDENCE_ADVANCE_REPORT.stderr"
    MORTGAGE_EVIDENCE_ADVANCE_RC=$?
    set -e
    if [ "$MORTGAGE_EVIDENCE_ADVANCE_RC" -ne 0 ]; then
      echo "[mortgage-workflow] Mortgage workflow evidence advance returned rc=$MORTGAGE_EVIDENCE_ADVANCE_RC; surfacing in review packet" >&2
    fi
  else
    echo "[mortgage-workflow] Mortgage workflow evidence advance script missing or not executable, skipping"
  fi
}

run_evidence_refresh() {
  if [ -x "$MORTGAGE_EVIDENCE_REFRESH_SCRIPT" ]; then
    echo "[mortgage-workflow] Refreshing mortgage workflow evidence analyses..."
    local evidence_refresh_timeout_seconds="${MORTGAGE_WORKFLOW_EVIDENCE_REFRESH_TIMEOUT_SECONDS:-$MORTGAGE_WORKFLOW_STEP_TIMEOUT_SECONDS}"
    local evidence_refresh_timeout_args=()
    if command -v timeout >/dev/null 2>&1 && [ "$evidence_refresh_timeout_seconds" != "0" ]; then
      evidence_refresh_timeout_args=(timeout --kill-after=10s "${evidence_refresh_timeout_seconds}s")
    fi
    set +e
    MORTGAGE_STATEMENT_TARGET_MONTH="$MORTGAGE_STATEMENT_TARGET_MONTH" \
    BASELANE_MORTGAGE_STATEMENT_TARGET_MONTH="$MORTGAGE_STATEMENT_TARGET_MONTH" \
    MORTGAGE_WORKFLOW_TARGET_MONTH="$MORTGAGE_STATEMENT_TARGET_MONTH" \
    CITADEL_STATEMENT_TARGET_MONTH="$DOWNLOADER_CITADEL_TARGET_MONTH" \
    "${evidence_refresh_timeout_args[@]}" "$PY" "$MORTGAGE_EVIDENCE_REFRESH_SCRIPT" \
      --ensure-handoffs \
      --target-month "$MORTGAGE_STATEMENT_TARGET_MONTH" \
      --report "$MORTGAGE_EVIDENCE_REFRESH_REPORT" \
      --report-dir "$REPORT_DIR" > "$MORTGAGE_EVIDENCE_REFRESH_REPORT.stdout" 2>"$MORTGAGE_EVIDENCE_REFRESH_REPORT.stderr"
    MORTGAGE_EVIDENCE_REFRESH_RC=$?
    set -e
    if [ "$MORTGAGE_EVIDENCE_REFRESH_RC" -ne 0 ]; then
      echo "[mortgage-workflow] Mortgage workflow evidence refresh returned rc=$MORTGAGE_EVIDENCE_REFRESH_RC; surfacing in review packet" >&2
      if [ "$MORTGAGE_EVIDENCE_REFRESH_RC" -eq 124 ] || [ "$MORTGAGE_EVIDENCE_REFRESH_RC" -eq 137 ]; then
        echo "[mortgage-workflow] Mortgage workflow evidence refresh timed out after ${evidence_refresh_timeout_seconds}s; preserving prior evidence analysis for this report-only pass" >&2
      fi
    fi
  else
    echo "[mortgage-workflow] Mortgage workflow evidence refresh script missing or not executable, skipping"
  fi
}

run_replayability_gate() {
  if [ -x "$MORTGAGE_REPLAYABILITY_SCRIPT" ]; then
    echo "[mortgage-workflow] Refreshing offline mortgage replayability gate..."
    set +e
    "${MORTGAGE_WORKFLOW_STEP_TIMEOUT_ARGS[@]}" "$PY" "$MORTGAGE_REPLAYABILITY_SCRIPT" \
      --coverage-report "$MORTGAGE_COVERAGE_REPORT" \
      --citadel-diagnostics-report "$CITADEL_HAR_DIAGNOSTICS_REPORT" \
      --analysis-dir "$REPORT_DIR" \
      --report "$MORTGAGE_REPLAYABILITY_REPORT" > "$MORTGAGE_REPLAYABILITY_REPORT.stdout" 2>"$MORTGAGE_REPLAYABILITY_REPORT.stderr"
    MORTGAGE_REPLAYABILITY_RC=$?
    set -e
    if [ "$MORTGAGE_REPLAYABILITY_RC" -ne 0 ]; then
      echo "[mortgage-workflow] Replayability gate returned rc=$MORTGAGE_REPLAYABILITY_RC; surfacing in review packet" >&2
    fi
  else
    echo "[mortgage-workflow] Replayability gate script missing or not executable, skipping"
  fi
}

run_capture_queue() {
  if [ -x "$MORTGAGE_CAPTURE_QUEUE_SCRIPT" ]; then
    echo "[mortgage-workflow] Refreshing idempotent mortgage capture queue..."
    set +e
    "${MORTGAGE_WORKFLOW_STEP_TIMEOUT_ARGS[@]}" "$PY" "$MORTGAGE_CAPTURE_QUEUE_SCRIPT" \
      --handoff-index "$REPORT_DIR/mortgage_workflow_evidence_handoff_index.json" \
      --evidence-refresh-report "$MORTGAGE_EVIDENCE_REFRESH_REPORT" \
      --tokenomics-report "$TOKENOMICS_REPORT" \
      --downloader-report "$MORTGAGE_DOWNLOADER_REPORT" \
      --coverage-report "$MORTGAGE_COVERAGE_REPORT" \
      --intake "$ROOT/config/mortgage_downloader_intake.json" \
      --target-month "$MORTGAGE_STATEMENT_TARGET_MONTH" \
      --workflow-target-month "$MORTGAGE_STATEMENT_TARGET_MONTH" \
      --report "$MORTGAGE_CAPTURE_QUEUE_REPORT" \
      --markdown "$MORTGAGE_CAPTURE_QUEUE_MARKDOWN" > "$MORTGAGE_CAPTURE_QUEUE_REPORT.stdout" 2>"$MORTGAGE_CAPTURE_QUEUE_REPORT.stderr"
    MORTGAGE_CAPTURE_QUEUE_RC=$?
    set -e
    if [ "$MORTGAGE_CAPTURE_QUEUE_RC" -ne 0 ]; then
      echo "[mortgage-workflow] Mortgage capture queue has pending visible-browser HAR work; surfacing in review packet" >&2
    fi
  else
    echo "[mortgage-workflow] Mortgage capture queue script missing or not executable, skipping"
  fi
}

run_capture_queue_processor() {
  if [ -x "$MORTGAGE_CAPTURE_QUEUE_PROCESSOR_SCRIPT" ]; then
    echo "[mortgage-workflow] Processing ready captured mortgage HARs idempotently..."
    local processor_args=(
      "--queue" "$MORTGAGE_CAPTURE_QUEUE_REPORT"
      "--report" "$MORTGAGE_CAPTURE_QUEUE_PROCESSOR_REPORT"
      "--markdown" "$MORTGAGE_CAPTURE_QUEUE_PROCESSOR_MARKDOWN"
      "--report-dir" "$REPORT_DIR"
      "--advance-script" "$MORTGAGE_EVIDENCE_ADVANCE_SCRIPT"
      "--coverage-report" "$MORTGAGE_COVERAGE_REPORT"
      "--workspace-root" "$ROOT"
      "--target-month" "$MORTGAGE_STATEMENT_TARGET_MONTH"
    )
    if [ "${MORTGAGE_WORKFLOW_CAPTURE_QUEUE_PROCESSOR_APPLY:-0}" = "1" ]; then
      processor_args+=("--apply")
    fi
    if [ "${MORTGAGE_WORKFLOW_CAPTURE_QUEUE_PROCESS_EXISTING_BODY_RECAPTURES:-0}" = "1" ]; then
      processor_args+=("--process-existing-body-recaptures")
    fi
    set +e
    "${MORTGAGE_WORKFLOW_STEP_TIMEOUT_ARGS[@]}" "$PY" "$MORTGAGE_CAPTURE_QUEUE_PROCESSOR_SCRIPT" "${processor_args[@]}" > "$MORTGAGE_CAPTURE_QUEUE_PROCESSOR_REPORT.stdout" 2>"$MORTGAGE_CAPTURE_QUEUE_PROCESSOR_REPORT.stderr"
    MORTGAGE_CAPTURE_QUEUE_PROCESSOR_RC=$?
    set -e
    if [ "$MORTGAGE_CAPTURE_QUEUE_PROCESSOR_RC" -ne 0 ]; then
      echo "[mortgage-workflow] Mortgage capture queue processor returned rc=$MORTGAGE_CAPTURE_QUEUE_PROCESSOR_RC; surfacing in review packet" >&2
    fi
  else
    echo "[mortgage-workflow] Mortgage capture queue processor script missing or not executable, skipping"
  fi
}

run_archive_restore() {
  if [ -x "$MORTGAGE_ARCHIVE_RESTORE_SCRIPT" ]; then
    echo "[mortgage-workflow] Restoring current-month co-owner mortgage statements from archive candidates..."
    local restore_args=(
      "--target-month" "$MORTGAGE_STATEMENT_TARGET_MONTH"
      "--current-month-only"
      "--report" "$MORTGAGE_ARCHIVE_RESTORE_REPORT"
      "--markdown" "$MORTGAGE_ARCHIVE_RESTORE_MARKDOWN"
      "--json"
    )
    if [ "${MORTGAGE_ARCHIVE_RESTORE_APPLY:-1}" = "1" ]; then
      restore_args+=("--apply")
    fi
    set +e
    "${MORTGAGE_WORKFLOW_STEP_TIMEOUT_ARGS[@]}" "$PY" "$MORTGAGE_ARCHIVE_RESTORE_SCRIPT" "${restore_args[@]}" > "$MORTGAGE_ARCHIVE_RESTORE_REPORT.stdout" 2>"$MORTGAGE_ARCHIVE_RESTORE_REPORT.stderr"
    MORTGAGE_ARCHIVE_RESTORE_RC=$?
    set -e
    if [ "$MORTGAGE_ARCHIVE_RESTORE_RC" -ne 0 ]; then
      echo "[mortgage-workflow] Mortgage archive restore returned rc=$MORTGAGE_ARCHIVE_RESTORE_RC; blocking workbook write" >&2
    fi
  else
    echo "[mortgage-workflow] Mortgage archive restore script missing or not executable, skipping"
  fi
}

run_report_only_citadel_har_diagnostics() {
  if [ -x "$CITADEL_HAR_DIAGNOSTICS_SCRIPT" ]; then
    echo "[mortgage-workflow] Report-only: refreshing Citadel HAR workflow diagnostics offline..."
    local citadel_har_path="${CITADEL_HAR_PATH:-}"
    if [ -z "$citadel_har_path" ]; then
      citadel_har_path="$("$PY" - "$ROOT/config/mortgage_statement_downloaders.json" <<'PY' || true
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(0)
for entry in data.get("downloaders") or []:
    if not isinstance(entry, dict):
        continue
    marker = " ".join(str(entry.get(key) or "") for key in ("id", "servicer", "script")).casefold()
    if "citadel" not in marker and "loansphere" not in marker:
        continue
    env = entry.get("env") if isinstance(entry.get("env"), dict) else {}
    har_path = str(env.get("CITADEL_HAR_PATH") or "").strip()
    if har_path:
        print(har_path)
        break
PY
)"
    fi
    set +e
    CITADEL_HAR_DIAGNOSTICS_REPORT="$CITADEL_HAR_DIAGNOSTICS_REPORT" \
    CITADEL_CAPTURE_PLAN_REPORT="$CITADEL_CAPTURE_PLAN_REPORT" \
    CITADEL_CAPTURE_PLAN_MARKDOWN="$CITADEL_CAPTURE_PLAN_MARKDOWN" \
    CITADEL_HAR_PATH="$citadel_har_path" \
    CITADEL_TARGET_MONTH="${CITADEL_TARGET_MONTH:-$DOWNLOADER_CITADEL_TARGET_MONTH}" \
    "${MORTGAGE_WORKFLOW_STEP_TIMEOUT_ARGS[@]}" "$NODE" "$CITADEL_HAR_DIAGNOSTICS_SCRIPT" > "$CITADEL_HAR_DIAGNOSTICS_REPORT.stdout" 2>"$CITADEL_HAR_DIAGNOSTICS_REPORT.stderr"
    CITADEL_HAR_DIAGNOSTICS_RC=$?
    set -e
    if [ "$CITADEL_HAR_DIAGNOSTICS_RC" -ne 0 ]; then
      echo "[mortgage-workflow] Report-only Citadel HAR diagnostic returned rc=$CITADEL_HAR_DIAGNOSTICS_RC; surfacing in review packet" >&2
    fi
  else
    echo "[mortgage-workflow] Report-only: Citadel HAR diagnostic script missing or not executable, using existing report if present"
  fi
}

configure_downloader_auto_profile_from_citadel_har_diagnostics() {
  if [ "${MORTGAGE_DOWNLOADER_AUTO_HAR_REPLAY:-1}" != "1" ]; then
    return 0
  fi
  if [ ! -f "$CITADEL_HAR_DIAGNOSTICS_REPORT" ]; then
    return 0
  fi
  local replay_ready
  replay_ready="$("$PY" - "$CITADEL_HAR_DIAGNOSTICS_REPORT" <<'PY'
import json
import sys
from pathlib import Path

try:
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except Exception:
    print("0")
    raise SystemExit(0)

ready = (
    data.get("status") == "ok"
    and data.get("capture_quality_status") == "replayable"
    and data.get("can_replay_documents") is True
    and not data.get("replay_blocker")
    and data.get("target_month_replayable_document_available") is True
)
print("1" if ready else "0")
PY
)"
  if [ "$replay_ready" = "1" ]; then
    DOWNLOADER_AUTO_PROFILE="${DOWNLOADER_AUTO_PROFILE:-har_replay}"
    DOWNLOADER_ALLOW_HAR_MODE=1
    echo "[mortgage-workflow] Citadel HAR diagnostic is replayable; enabling guarded har_replay downloader profile"
  fi
}

install_json_report_atomically() {
  local source="$1"
  local target="$2"
  local tmp
  if [ ! -s "$source" ]; then
    return 1
  fi
  "$PY" - "$source" <<'PY'
import json
import sys
from pathlib import Path

with Path(sys.argv[1]).open(encoding="utf-8") as handle:
    json.load(handle)
PY
  if [ "$?" -ne 0 ]; then
    return 1
  fi
  tmp="$(mktemp "${target}.tmp.XXXXXX")" || return 1
  if ! cp "$source" "$tmp"; then
    rm -f "$tmp"
    return 1
  fi
  mv "$tmp" "$target"
}

run_tokenomics_json_report() {
  set +e
  "${MORTGAGE_WORKFLOW_STEP_TIMEOUT_ARGS[@]}" "$PY" "$TOKENOMICS_SCRIPT" --json --month "$MORTGAGE_STATEMENT_TARGET_MONTH" --coverage-report "$MORTGAGE_COVERAGE_REPORT" --report "$TOKENOMICS_REPORT" > "$TOKENOMICS_REPORT.stdout" 2>"$TOKENOMICS_REPORT.stderr"
  local rc=$?
  if [ "$rc" -eq 0 ] && [ ! -s "$TOKENOMICS_REPORT" ] && [ -s "$TOKENOMICS_REPORT.stdout" ]; then
    install_json_report_atomically "$TOKENOMICS_REPORT.stdout" "$TOKENOMICS_REPORT" || rc=$?
  fi
  if [ "$rc" -ne 0 ] && grep -q -- "--report" "$TOKENOMICS_REPORT.stderr" 2>/dev/null; then
    local fallback_report
    fallback_report="$(mktemp "${TOKENOMICS_REPORT}.fallback.XXXXXX")"
    "${MORTGAGE_WORKFLOW_STEP_TIMEOUT_ARGS[@]}" "$PY" "$TOKENOMICS_SCRIPT" --json --month "$MORTGAGE_STATEMENT_TARGET_MONTH" --coverage-report "$MORTGAGE_COVERAGE_REPORT" > "$fallback_report" 2>"$TOKENOMICS_REPORT.stderr"
    rc=$?
    if [ "$rc" -eq 0 ]; then
      install_json_report_atomically "$fallback_report" "$TOKENOMICS_REPORT" || rc=$?
      cp "$TOKENOMICS_REPORT" "$TOKENOMICS_REPORT.stdout" 2>/dev/null || true
    fi
    rm -f "$fallback_report"
  fi
  return "$rc"
}

run_report_only_tokenomics_diagnostic() {
  if [ -x "$TOKENOMICS_SCRIPT" ]; then
    if "$PY" -c 'import openpyxl, pypdf' >/dev/null 2>&1; then
      echo "[mortgage-workflow] Report-only: refreshing Co-Ownership Mortgage Tokenomics diagnostic JSON..."
      set +e
      run_tokenomics_json_report
      TOKENOMICS_RC=$?
      set -e
      if [ "$TOKENOMICS_RC" -ne 0 ]; then
        echo "[mortgage-workflow] Report-only tokenomics diagnostic returned rc=$TOKENOMICS_RC; surfacing existing report if present" >&2
      fi
    else
      echo "[mortgage-workflow] Report-only: skipping tokenomics diagnostic: openpyxl/pypdf not installed" >&2
    fi
  else
    echo "[mortgage-workflow] Report-only: tokenomics script missing or not executable, using existing report if present"
  fi
}

run_report_only_mortgage_splits_diagnostic() {
  if [ -x "$MORTGAGE_SPLITS_SCRIPT" ]; then
    echo "[mortgage-workflow] Report-only: refreshing Baselane mortgage split diagnostic JSON..."
    set +e
    "${MORTGAGE_WORKFLOW_STEP_TIMEOUT_ARGS[@]}" "$PY" "$MORTGAGE_SPLITS_SCRIPT" --json > "$MORTGAGE_SPLITS_REPORT" 2>"$MORTGAGE_SPLITS_REPORT.stderr"
    SPLITS_RC=$?
    set -e
    if [ "$SPLITS_RC" -ne 0 ]; then
      echo "[mortgage-workflow] Report-only mortgage split diagnostic returned rc=$SPLITS_RC; surfacing in review packet" >&2
    fi
  else
    echo "[mortgage-workflow] Report-only: mortgage splits script missing or not executable, using existing report if present"
  fi
}

run_report_only_no_mortgage_financials_guard() {
  if [ -x "$NO_MORTGAGE_FINANCIALS_GUARD_SCRIPT" ]; then
    echo "[mortgage-workflow] Report-only: refreshing no-DAO mortgage financials guard..."
    set +e
      "${MORTGAGE_WORKFLOW_STEP_TIMEOUT_ARGS[@]}" "$PY" "$NO_MORTGAGE_FINANCIALS_GUARD_SCRIPT" \
      --states "${BASELANE_NO_MORTGAGE_FINANCIALS_GUARD_STATES:-NY,HI,OH,TN,IL}" \
      --report "$NO_MORTGAGE_FINANCIALS_GUARD_REPORT" \
      > "$NO_MORTGAGE_FINANCIALS_GUARD_REPORT.stdout" \
      2>"$NO_MORTGAGE_FINANCIALS_GUARD_REPORT.stderr"
    NO_MORTGAGE_FINANCIALS_GUARD_RC=$?
    set -e
    if [ "$NO_MORTGAGE_FINANCIALS_GUARD_RC" -ne 0 ]; then
      echo "[mortgage-workflow] Report-only no-DAO mortgage financials guard returned rc=$NO_MORTGAGE_FINANCIALS_GUARD_RC; surfacing in review packet" >&2
    fi
  else
    echo "[mortgage-workflow] Report-only: no-DAO mortgage financials guard missing or not executable, using existing report if present"
  fi
}

run_report_only_mortgage_downloader() {
  if [ -x "$MORTGAGE_DOWNLOADER_RUNNER" ]; then
    if [ "${MORTGAGE_WORKFLOW_REPORT_ONLY_REFRESH_CREDENTIALS:-1}" = "1" ]; then
      local credential_profile="${MORTGAGE_WORKFLOW_REPORT_ONLY_CREDENTIAL_REFRESH_PROFILE:-credential_check}"
      local credential_ids="${MORTGAGE_WORKFLOW_REPORT_ONLY_CREDENTIAL_REFRESH_IDS:-generated-85-104-alawa-pl,mortgagequestions-86-madison}"
      MORTGAGE_CREDENTIAL_REFRESH_PROFILE="$credential_profile"
      MORTGAGE_CREDENTIAL_REFRESH_IDS="$credential_ids"
      MORTGAGE_CREDENTIAL_REFRESH_STATUS="not_run"
      local credential_id_args=()
      local credential_id
      for credential_id in ${credential_ids//,/ }; do
        if [ -n "$credential_id" ]; then
          credential_id_args+=(--id "$credential_id")
        fi
      done
      if [ -n "$credential_profile" ] && [ "${#credential_id_args[@]}" -gt 0 ]; then
        echo "[mortgage-workflow] Report-only: refreshing mortgage downloader credential preflight profile=$credential_profile ids=$credential_ids"
        local credential_timeout_seconds="${MORTGAGE_WORKFLOW_REPORT_ONLY_CREDENTIAL_TIMEOUT_SECONDS:-180}"
        MORTGAGE_CREDENTIAL_REFRESH_TIMEOUT_SECONDS="$credential_timeout_seconds"
        local credential_timeout_args=()
        if command -v timeout >/dev/null 2>&1 && [ "$credential_timeout_seconds" != "0" ]; then
          credential_timeout_args=(timeout --kill-after=10s "${credential_timeout_seconds}s")
        fi
        local credential_citadel_target_month=""
        if [ "${MORTGAGE_WORKFLOW_REPORT_ONLY_CREDENTIAL_USE_EXPLICIT_CITADEL_TARGET_MONTH:-0}" = "1" ]; then
          credential_citadel_target_month="$DOWNLOADER_CITADEL_TARGET_MONTH"
        fi
        set +e
        CITADEL_TARGET_MONTH="$credential_citadel_target_month" \
        CITADEL_STATEMENT_TARGET_MONTH="$credential_citadel_target_month" \
        MORTGAGE_DOWNLOADER_CITADEL_TARGET_MONTH="$credential_citadel_target_month" \
        MORTGAGE_STATEMENT_TARGET_MONTH="$MORTGAGE_STATEMENT_TARGET_MONTH" \
        BASELANE_MORTGAGE_STATEMENT_TARGET_MONTH="$MORTGAGE_STATEMENT_TARGET_MONTH" \
        MORTGAGE_DOWNLOADER_EXACT_TARGET_MONTH="${MORTGAGE_WORKFLOW_REPORT_ONLY_CREDENTIAL_EXACT_TARGET_MONTH:-1}" \
        MORTGAGE_DOWNLOADER_PROFILE="$credential_profile" \
        MORTGAGE_DOWNLOADER_ALLOW_HAR_MODE="${MORTGAGE_DOWNLOADER_ALLOW_HAR_MODE:-$DOWNLOADER_ALLOW_HAR_MODE}" \
        "${credential_timeout_args[@]}" "$PY" "$MORTGAGE_DOWNLOADER_RUNNER" \
          --profile "$credential_profile" \
          "${credential_id_args[@]}" \
          --target-month "$MORTGAGE_STATEMENT_TARGET_MONTH" \
          --report "$MORTGAGE_CREDENTIAL_REFRESH_REPORT" \
          > "$MORTGAGE_CREDENTIAL_REFRESH_REPORT.stdout" \
          2>"$MORTGAGE_CREDENTIAL_REFRESH_REPORT.stderr"
        local credential_refresh_rc=$?
        MORTGAGE_CREDENTIAL_REFRESH_RC="$credential_refresh_rc"
        set -e
        if [ "$credential_refresh_rc" -ne 0 ]; then
          MORTGAGE_CREDENTIAL_REFRESH_STATUS="review"
          echo "[mortgage-workflow] Report-only credential preflight returned rc=$credential_refresh_rc; aggregate will surface current report state" >&2
          if [ "$credential_refresh_rc" -eq 124 ] || [ "$credential_refresh_rc" -eq 137 ]; then
            MORTGAGE_CREDENTIAL_REFRESH_STATUS="timeout"
            echo "[mortgage-workflow] Report-only credential preflight timed out after ${credential_timeout_seconds}s; aggregate will preserve prior credential state" >&2
          fi
        else
          MORTGAGE_CREDENTIAL_REFRESH_STATUS="ok"
        fi
      fi
    else
      MORTGAGE_CREDENTIAL_REFRESH_STATUS="disabled"
    fi
    echo "[mortgage-workflow] Report-only: refreshing mortgage statement downloader aggregate from existing per-downloader reports"
    local profile="${MORTGAGE_DOWNLOADER_PROFILE:-$DOWNLOADER_AUTO_PROFILE}"
    local profile_args=()
    if [ -n "$profile" ]; then
      profile_args=(--profile "$profile")
    fi
    local report_only_citadel_target_month=""
    if [ "${MORTGAGE_WORKFLOW_REPORT_ONLY_USE_EXPLICIT_CITADEL_TARGET_MONTH:-0}" = "1" ]; then
      report_only_citadel_target_month="$DOWNLOADER_CITADEL_TARGET_MONTH"
    fi
    set +e
    CITADEL_TARGET_MONTH="$report_only_citadel_target_month" \
    CITADEL_STATEMENT_TARGET_MONTH="$report_only_citadel_target_month" \
    MORTGAGE_DOWNLOADER_CITADEL_TARGET_MONTH="$report_only_citadel_target_month" \
    MORTGAGE_STATEMENT_TARGET_MONTH="$MORTGAGE_STATEMENT_TARGET_MONTH" \
    BASELANE_MORTGAGE_STATEMENT_TARGET_MONTH="$MORTGAGE_STATEMENT_TARGET_MONTH" \
    MORTGAGE_DOWNLOADER_EXACT_TARGET_MONTH="${MORTGAGE_WORKFLOW_REPORT_ONLY_DOWNLOADER_EXACT_TARGET_MONTH:-1}" \
    MORTGAGE_DOWNLOADER_PROFILE="$profile" \
    MORTGAGE_DOWNLOADER_ALLOW_HAR_MODE="${MORTGAGE_DOWNLOADER_ALLOW_HAR_MODE:-$DOWNLOADER_ALLOW_HAR_MODE}" \
    "${MORTGAGE_WORKFLOW_STEP_TIMEOUT_ARGS[@]}" "$PY" "$MORTGAGE_DOWNLOADER_RUNNER" \
      --summarize-existing \
      "${profile_args[@]}" \
      --target-month "$MORTGAGE_STATEMENT_TARGET_MONTH" \
      --report "$MORTGAGE_DOWNLOADER_REPORT" \
      > "$MORTGAGE_DOWNLOADER_REPORT.stdout" 2>"$MORTGAGE_DOWNLOADER_REPORT.stderr"
    DOWNLOADER_RC=$?
    MORTGAGE_DOWNLOADER_REPORT_ONLY_SUMMARIZED=1
    set -e
    if [ "$DOWNLOADER_RC" -ne 0 ]; then
      echo "[mortgage-workflow] Report-only mortgage downloader aggregate returned rc=$DOWNLOADER_RC; surfacing in review packet" >&2
    fi
  elif [ -f "$MORTGAGE_DOWNLOADER_REPORT" ]; then
    echo "[mortgage-workflow] Report-only: downloader runner missing; using existing mortgage statement downloader aggregate"
    CITADEL_RC="$("$PY" - "$MORTGAGE_DOWNLOADER_REPORT" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
    print(int(data.get("citadel_download_rc") or 0))
except Exception:
    print(2)
PY
)"
  else
    echo "[mortgage-workflow] Report-only: mortgage downloader aggregate missing and runner unavailable; live downloader execution skipped"
  fi
  if [ -f "$MORTGAGE_DOWNLOADER_REPORT" ]; then
    CITADEL_RC="$("$PY" - "$MORTGAGE_DOWNLOADER_REPORT" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
    print(int(data.get("citadel_download_rc") or 0))
except Exception:
    print(2)
PY
)"
  fi
}

run_report_only_citadel_har_replay_downloader() {
  if [ "$DOWNLOADER_AUTO_PROFILE" != "har_replay" ] || [ "$DOWNLOADER_ALLOW_HAR_MODE" != "1" ]; then
    return 0
  fi
  if [ "$MORTGAGE_WORKFLOW_REPORT_ONLY_RUN_HAR_REPLAY" != "1" ]; then
    echo "[mortgage-workflow] Report-only: skipping Citadel guarded HAR replay downloader; set MORTGAGE_WORKFLOW_REPORT_ONLY_RUN_HAR_REPLAY=1 to refresh it"
    return 0
  fi
  if [ ! -x "$MORTGAGE_DOWNLOADER_RUNNER" ]; then
    return 0
  fi
  local profile="${MORTGAGE_DOWNLOADER_PROFILE:-$DOWNLOADER_AUTO_PROFILE}"
  local allow_har="${MORTGAGE_DOWNLOADER_ALLOW_HAR_MODE:-$DOWNLOADER_ALLOW_HAR_MODE}"
  local timeout_args=()
  if command -v timeout >/dev/null 2>&1 && [ "$MORTGAGE_WORKFLOW_REPORT_ONLY_HAR_REPLAY_TIMEOUT_SECONDS" != "0" ]; then
    timeout_args=(timeout --kill-after=10s "${MORTGAGE_WORKFLOW_REPORT_ONLY_HAR_REPLAY_TIMEOUT_SECONDS}s")
  fi
  echo "[mortgage-workflow] Report-only: refreshing Citadel guarded HAR replay downloader report offline"
  set +e
  MORTGAGE_DOWNLOADER_REPORT="$CITADEL_HAR_REPLAY_RUNNER_REPORT" \
  MORTGAGE_DOWNLOADER_PROFILE="$profile" \
  MORTGAGE_DOWNLOADER_ALLOW_HAR_MODE="$allow_har" \
  CITADEL_TARGET_MONTH="${CITADEL_TARGET_MONTH:-$DOWNLOADER_CITADEL_TARGET_MONTH}" \
  MORTGAGE_STATEMENT_TARGET_MONTH="$MORTGAGE_STATEMENT_TARGET_MONTH" \
  BASELANE_MORTGAGE_STATEMENT_TARGET_MONTH="$MORTGAGE_STATEMENT_TARGET_MONTH" \
  MORTGAGE_DOWNLOADER_EXACT_TARGET_MONTH=1 \
  MORTGAGE_DOWNLOADER_CITADEL_TARGET_MONTH="$DOWNLOADER_CITADEL_TARGET_MONTH" \
  "${timeout_args[@]}" "$PY" "$MORTGAGE_DOWNLOADER_RUNNER" \
    --id citadel-90-madison \
    --profile "$profile" \
    --target-month "$MORTGAGE_STATEMENT_TARGET_MONTH" \
    --report "$CITADEL_HAR_REPLAY_RUNNER_REPORT" \
    > "$CITADEL_HAR_REPLAY_RUNNER_REPORT.stdout" 2>"$CITADEL_HAR_REPLAY_RUNNER_REPORT.stderr"
  local har_replay_rc=$?
  set -e
  if [ "$har_replay_rc" -ne 0 ]; then
    echo "[mortgage-workflow] Report-only Citadel HAR replay downloader returned rc=$har_replay_rc; aggregate will surface current report state" >&2
  fi
}

run_live_tokenomics_diagnostic() {
  if [ -x "$TOKENOMICS_SCRIPT" ]; then
    if "$PY" -c 'import openpyxl, pypdf' >/dev/null 2>&1; then
      echo "[mortgage-workflow] Refreshing Co-Ownership Mortgage Tokenomics diagnostic JSON before write guard..."
      set +e
      run_tokenomics_json_report
      TOKENOMICS_RC=$?
      set -e
      if [ "$TOKENOMICS_RC" -ne 0 ]; then
        echo "[mortgage-workflow] Tokenomics diagnostic returned rc=$TOKENOMICS_RC; blocking workbook write" >&2
      fi
    else
      echo "[mortgage-workflow] Skipping tokenomics diagnostic: openpyxl/pypdf not installed" >&2
      TOKENOMICS_RC=1
    fi
  else
    echo "[mortgage-workflow] Tokenomics script missing or not executable, blocking workbook write"
    TOKENOMICS_RC=1
  fi
}

tokenomics_diagnostic_safe() {
  "$PY" - "$TOKENOMICS_REPORT" <<'PY'
import json
import sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        data = json.load(f)
    print("true" if data.get("safe_to_run_automatically") is True else "false")
except Exception:
    print("false")
PY
}

tokenomics_diagnostic_review() {
  "$PY" - "$TOKENOMICS_REPORT" <<'PY'
import json
import sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        data = json.load(f)
except Exception:
    print("false")
    raise SystemExit(0)

status = str(data.get("status") or "").strip().upper()
classification = str(data.get("classification") or "").strip().lower()
gate_status = str(data.get("workbook_write_gate_status") or "").strip().lower()
print("true" if classification == "review" or gate_status == "review" or status.endswith("_REVIEW") else "false")
PY
}

capture_queue_tokenomics_guard_summary() {
  "$PY" - "$TOKENOMICS_REPORT" "$MORTGAGE_CAPTURE_QUEUE_REPORT" <<'PY'
import json
import sys

tokenomics_path, capture_path = sys.argv[1], sys.argv[2]

def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def ordered_unique(values):
    result = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result

tokenomics = load(tokenomics_path)
capture = load(capture_path)
gap_properties = ordered_unique(
    tokenomics.get("current_month_statement_gap_properties")
    or tokenomics.get("missing_co_owner_paid_statement_properties")
    or []
)
gap_set = set(gap_properties)

entry_properties = []
entries = capture.get("entries")
if isinstance(entries, list):
    entry_properties.extend(
        item.get("property")
        for item in entries
        if isinstance(item, dict)
    )
entry_properties.extend(capture.get("capture_properties") or [])
capture_properties = ordered_unique(entry_properties)

blocking = [prop for prop in capture_properties if prop in gap_set]
non_blocking = [prop for prop in capture_properties if prop not in gap_set]
print(json.dumps({
    "blocking": blocking,
    "non_blocking": non_blocking,
}, separators=(",", ":")))
PY
}

downloader_tokenomics_guard_summary() {
  "$PY" - "$MORTGAGE_DOWNLOADER_REPORT" "$DOWNLOADER_RC" "$CITADEL_RC" <<'PY'
import json
import sys

report_path, downloader_rc, citadel_rc = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
try:
    with open(report_path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        data = {}
except Exception:
    data = {}

gap_properties = data.get("target_month_statement_gap_properties")
if not isinstance(gap_properties, list):
    gap_properties = []
try:
    gap_count = int(data.get("target_month_statement_gap_count"))
except Exception:
    gap_count = len(gap_properties)
status_counts = data.get("downloader_status_counts")
if not isinstance(status_counts, dict):
    status_counts = {}
try:
    failed_count = int(data.get("failed_count") or 0)
except Exception:
    failed_count = 0
try:
    error_count = int(data.get("error_count") or 0)
except Exception:
    error_count = 0
failed_status_count = 0
for key in ("failed", "error"):
    try:
        failed_status_count += int(status_counts.get(key) or 0)
    except Exception:
        pass
downloader_failed = downloader_rc != 0 and (
    not data or failed_count > 0 or error_count > 0 or failed_status_count > 0
)
if downloader_rc != 0 and not data:
    gap_count = max(gap_count, 1)
downloader_review = downloader_rc != 0 and not downloader_failed

citadel_available = data.get("citadel_target_month_statement_available") is True
citadel_blocking = citadel_rc != 0 and not citadel_available
print(json.dumps({
    "downloader_blocking_gap_count": gap_count if downloader_rc != 0 else 0,
    "downloader_blocking_gap_properties": gap_properties if downloader_rc != 0 and gap_count > 0 else [],
    "downloader_failed": downloader_failed,
    "downloader_review": downloader_review,
    "citadel_target_month_statement_available": citadel_available,
    "citadel_blocking": citadel_blocking,
}, separators=(",", ":")))
PY
}

tokenomics_truth_ready_summary() {
  "$PY" - "$TOKENOMICS_REPORT" <<'PY'
import json
import sys

path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        data = {}
except Exception:
    data = {}

def intish(key):
    try:
        return int(data.get(key) or 0)
    except Exception:
        return 0

truth_ready = (
    bool(data)
    and data.get("safe_to_run_automatically") is not False
    and data.get("workbook_write_ready") is not False
    and intish("workbook_write_blocker_count") == 0
    and intish("current_month_statement_gap_count") == 0
    and intish("missing_current_month_statement_count") == 0
    and intish("stale_statement_count") == 0
)
print(json.dumps({
    "truth_ready": truth_ready,
    "workbook_write_ready": data.get("workbook_write_ready"),
    "workbook_write_blocker_count": intish("workbook_write_blocker_count"),
    "current_month_statement_gap_count": intish("current_month_statement_gap_count"),
    "missing_current_month_statement_count": intish("missing_current_month_statement_count"),
    "stale_statement_count": intish("stale_statement_count"),
    "ready_property_write_plan_count": intish("workbook_ready_property_write_plan_count"),
}, separators=(",", ":")))
PY
}

evaluate_tokenomics_write_guard() {
  local reasons=()
  local capture_queue_guard_summary='{"blocking":[],"non_blocking":[]}'
  local downloader_guard_summary='{"downloader_blocking_gap_count":0,"downloader_blocking_gap_properties":[],"citadel_blocking":false}'
  local tokenomics_truth_summary='{"truth_ready":false}'
  TOKENOMICS_WRITE_GUARD_DOWNLOADER_BLOCKING_GAP_COUNT=0
  TOKENOMICS_WRITE_GUARD_DOWNLOADER_BLOCKING_GAP_PROPERTIES="[]"
  TOKENOMICS_WRITE_GUARD_DOWNLOADER_FAILED=false
  TOKENOMICS_WRITE_GUARD_DOWNLOADER_REVIEW=false
  TOKENOMICS_WRITE_GUARD_CITADEL_BLOCKING=false
  TOKENOMICS_WRITE_GUARD_CAPTURE_QUEUE_BLOCKING_COUNT=0
  TOKENOMICS_WRITE_GUARD_CAPTURE_QUEUE_BLOCKING_PROPERTIES="[]"
  TOKENOMICS_WRITE_GUARD_CAPTURE_QUEUE_NON_BLOCKING_COUNT=0
  TOKENOMICS_WRITE_GUARD_CAPTURE_QUEUE_NON_BLOCKING_PROPERTIES="[]"
  TOKENOMICS_WRITE_GUARD_TRUTH_READY=false
  TOKENOMICS_WRITE_GUARD_INFRA_REVIEW_IGNORED=false

  tokenomics_truth_summary="$(tokenomics_truth_ready_summary || printf '{"truth_ready":false}')"
  TOKENOMICS_WRITE_GUARD_TRUTH_READY="$(
    "$PY" - "$tokenomics_truth_summary" <<'PY'
import json, sys
try:
    print("true" if json.loads(sys.argv[1]).get("truth_ready") is True else "false")
except Exception:
    print("false")
PY
  )"

  if [ "$DOWNLOADER_RC" -ne 0 ] || [ "$CITADEL_RC" -ne 0 ]; then
    downloader_guard_summary="$(downloader_tokenomics_guard_summary || printf '{"downloader_blocking_gap_count":1,"downloader_blocking_gap_properties":[],"citadel_blocking":true}')"
    TOKENOMICS_WRITE_GUARD_DOWNLOADER_BLOCKING_GAP_COUNT="$(
      "$PY" - "$downloader_guard_summary" <<'PY'
import json, sys
try:
    print(int(json.loads(sys.argv[1]).get("downloader_blocking_gap_count") or 0))
except Exception:
    print(1)
PY
    )"
    TOKENOMICS_WRITE_GUARD_DOWNLOADER_BLOCKING_GAP_PROPERTIES="$(
      "$PY" - "$downloader_guard_summary" <<'PY'
import json, sys
try:
    print(json.dumps(json.loads(sys.argv[1]).get("downloader_blocking_gap_properties") or []))
except Exception:
    print("[]")
PY
    )"
    TOKENOMICS_WRITE_GUARD_CITADEL_BLOCKING="$(
      "$PY" - "$downloader_guard_summary" <<'PY'
import json, sys
try:
    print("true" if json.loads(sys.argv[1]).get("citadel_blocking") is True else "false")
except Exception:
    print("true")
PY
    )"
    TOKENOMICS_WRITE_GUARD_DOWNLOADER_FAILED="$(
      "$PY" - "$downloader_guard_summary" <<'PY'
import json, sys
try:
    print("true" if json.loads(sys.argv[1]).get("downloader_failed") is True else "false")
except Exception:
    print("true")
PY
    )"
    TOKENOMICS_WRITE_GUARD_DOWNLOADER_REVIEW="$(
      "$PY" - "$downloader_guard_summary" <<'PY'
import json, sys
try:
    print("true" if json.loads(sys.argv[1]).get("downloader_review") is True else "false")
except Exception:
    print("false")
PY
    )"
    if [ "$TOKENOMICS_WRITE_GUARD_TRUTH_READY" = "true" ] && [ "${MORTGAGE_WORKFLOW_REPORT_ONLY:-0}" = "1" ]; then
      TOKENOMICS_WRITE_GUARD_DOWNLOADER_BLOCKING_GAP_COUNT=0
      TOKENOMICS_WRITE_GUARD_DOWNLOADER_BLOCKING_GAP_PROPERTIES="[]"
      TOKENOMICS_WRITE_GUARD_CITADEL_BLOCKING=false
      TOKENOMICS_WRITE_GUARD_DOWNLOADER_FAILED=false
      TOKENOMICS_WRITE_GUARD_DOWNLOADER_REVIEW=false
    fi
  fi

  if [ "${MORTGAGE_CAPTURE_QUEUE_RC:-0}" -ne 0 ]; then
    capture_queue_guard_summary="$(capture_queue_tokenomics_guard_summary || printf '{"blocking":[],"non_blocking":[]}')"
    TOKENOMICS_WRITE_GUARD_CAPTURE_QUEUE_BLOCKING_PROPERTIES="$(
      "$PY" - "$capture_queue_guard_summary" <<'PY'
import json, sys
try:
    print(json.dumps(json.loads(sys.argv[1]).get("blocking") or []))
except Exception:
    print("[]")
PY
    )"
    TOKENOMICS_WRITE_GUARD_CAPTURE_QUEUE_NON_BLOCKING_PROPERTIES="$(
      "$PY" - "$capture_queue_guard_summary" <<'PY'
import json, sys
try:
    print(json.dumps(json.loads(sys.argv[1]).get("non_blocking") or []))
except Exception:
    print("[]")
PY
    )"
    TOKENOMICS_WRITE_GUARD_CAPTURE_QUEUE_BLOCKING_COUNT="$(
      "$PY" - "$TOKENOMICS_WRITE_GUARD_CAPTURE_QUEUE_BLOCKING_PROPERTIES" <<'PY'
import json, sys
try:
    print(len(json.loads(sys.argv[1]) or []))
except Exception:
    print(0)
PY
    )"
    TOKENOMICS_WRITE_GUARD_CAPTURE_QUEUE_NON_BLOCKING_COUNT="$(
      "$PY" - "$TOKENOMICS_WRITE_GUARD_CAPTURE_QUEUE_NON_BLOCKING_PROPERTIES" <<'PY'
import json, sys
try:
    print(len(json.loads(sys.argv[1]) or []))
except Exception:
    print(0)
PY
    )"
  fi

  if [ "$TOKENOMICS_WRITE_GUARD_TRUTH_READY" != "true" ] || [ "${MORTGAGE_WORKFLOW_REPORT_ONLY:-0}" != "1" ]; then
    if [ "$TOKENOMICS_RC" -ne 0 ] && [ "$(tokenomics_diagnostic_review)" = "true" ]; then
      reasons+=("tokenomics-diagnostic-review")
    elif [ "$TOKENOMICS_RC" -ne 0 ]; then
      reasons+=("tokenomics-diagnostic-failed")
    elif [ "$(tokenomics_diagnostic_safe)" != "true" ]; then
      reasons+=("tokenomics-diagnostic-review")
    fi
  elif [ "$TOKENOMICS_RC" -ne 0 ] || [ "$(tokenomics_diagnostic_safe)" != "true" ]; then
    TOKENOMICS_WRITE_GUARD_INFRA_REVIEW_IGNORED=true
  fi
  if [ "$DOWNLOADER_RC" -ne 0 ] && [ "${TOKENOMICS_WRITE_GUARD_DOWNLOADER_FAILED:-false}" = "true" ] && [ "$TOKENOMICS_WRITE_GUARD_TRUTH_READY" != "true" ]; then
    reasons+=("mortgage-downloader-failed")
  elif [ "$DOWNLOADER_RC" -ne 0 ] && [ "${TOKENOMICS_WRITE_GUARD_DOWNLOADER_REVIEW:-false}" = "true" ] && [ "${TOKENOMICS_WRITE_GUARD_DOWNLOADER_BLOCKING_GAP_COUNT:-0}" -gt 0 ] && [ "$TOKENOMICS_WRITE_GUARD_TRUTH_READY" != "true" ]; then
    reasons+=("mortgage-downloader-review")
  fi
  if [ "$CITADEL_RC" -ne 0 ] && [ "${TOKENOMICS_WRITE_GUARD_CITADEL_BLOCKING:-false}" = "true" ] && [ "$TOKENOMICS_WRITE_GUARD_TRUTH_READY" != "true" ]; then
    reasons+=("citadel-download-failed")
  fi
  if [ "$COVERAGE_RC" -ne 0 ] && { [ "$TOKENOMICS_WRITE_GUARD_TRUTH_READY" != "true" ] || [ "${MORTGAGE_WORKFLOW_REPORT_ONLY:-0}" != "1" ]; }; then
    reasons+=("mortgage-coverage-review")
  elif [ "$COVERAGE_RC" -ne 0 ]; then
    TOKENOMICS_WRITE_GUARD_INFRA_REVIEW_IGNORED=true
  fi
  if [ "${MORTGAGE_ARCHIVE_RESTORE_RC:-0}" -ne 0 ]; then
    reasons+=("mortgage-archive-restore-review")
  fi
  if [ "${MORTGAGE_EVIDENCE_ADVANCE_RC:-0}" -ne 0 ] && [ "$TOKENOMICS_WRITE_GUARD_TRUTH_READY" != "true" ]; then
    reasons+=("mortgage-evidence-advance-review")
  fi
  if [ "${MORTGAGE_EVIDENCE_REFRESH_RC:-0}" -ne 0 ] && [ "$TOKENOMICS_WRITE_GUARD_TRUTH_READY" != "true" ]; then
    reasons+=("mortgage-evidence-refresh-review")
  fi
  if [ "$MORTGAGE_REPLAYABILITY_RC" -ne 0 ] && { [ "$TOKENOMICS_WRITE_GUARD_TRUTH_READY" != "true" ] || [ "${MORTGAGE_WORKFLOW_REPORT_ONLY:-0}" != "1" ]; }; then
    reasons+=("mortgage-replayability-review")
  elif [ "$MORTGAGE_REPLAYABILITY_RC" -ne 0 ]; then
    TOKENOMICS_WRITE_GUARD_INFRA_REVIEW_IGNORED=true
  fi
  if [ "$MORTGAGE_CAPTURE_QUEUE_RC" -ne 0 ] && [ "${TOKENOMICS_WRITE_GUARD_CAPTURE_QUEUE_BLOCKING_COUNT:-0}" -gt 0 ]; then
    reasons+=("mortgage-capture-queue-review")
  fi

  if [ "${#reasons[@]}" -eq 0 ]; then
    if [ "$TOKENOMICS_WRITE_GUARD_TRUTH_READY" = "true" ] && {
      [ "$COVERAGE_RC" -ne 0 ] || [ "${MORTGAGE_EVIDENCE_ADVANCE_RC:-0}" -ne 0 ] || [ "${MORTGAGE_EVIDENCE_REFRESH_RC:-0}" -ne 0 ] || [ "$MORTGAGE_REPLAYABILITY_RC" -ne 0 ]
    }; then
      TOKENOMICS_WRITE_GUARD_INFRA_REVIEW_IGNORED=true
    fi
    TOKENOMICS_WRITE_GUARD_STATUS="ready"
    TOKENOMICS_WRITE_GUARD_REASON=""
    return 0
  fi

  local joined=""
  local reason
  for reason in "${reasons[@]}"; do
    if [ -n "$joined" ]; then
      joined="${joined};${reason}"
    else
      joined="$reason"
    fi
  done
  TOKENOMICS_WRITE_GUARD_STATUS="blocked"
  TOKENOMICS_WRITE_GUARD_REASON="$joined"
  TOKENOMICS_RC=1
  return 1
}

run_report_only_downloader_coverage() {
  if [ -x "$MORTGAGE_COVERAGE_SCRIPT" ]; then
    if "$PY" -c 'import openpyxl, pypdf' >/dev/null 2>&1; then
      echo "[mortgage-workflow] Report-only: refreshing co-owner-paid mortgage downloader coverage..."
      set +e
      "${MORTGAGE_WORKFLOW_STEP_TIMEOUT_ARGS[@]}" "$PY" "$MORTGAGE_COVERAGE_SCRIPT" --month "$MORTGAGE_STATEMENT_TARGET_MONTH" --report "$MORTGAGE_COVERAGE_REPORT" > "$MORTGAGE_COVERAGE_REPORT.stdout" 2>"$MORTGAGE_COVERAGE_REPORT.stderr"
      COVERAGE_RC=$?
      set -e
      if [ "$COVERAGE_RC" -ne 0 ]; then
        echo "[mortgage-workflow] Report-only downloader coverage audit returned rc=$COVERAGE_RC; surfacing in review packet" >&2
      fi
    else
      echo "[mortgage-workflow] Report-only: skipping downloader coverage audit: openpyxl/pypdf not installed" >&2
    fi
  else
    echo "[mortgage-workflow] Report-only: downloader coverage audit script missing or not executable, using existing report if present"
  fi
}

if [ "${MORTGAGE_WORKFLOW_REPORT_ONLY:-0}" = "1" ]; then
  DOWNLOADER_RC="${MORTGAGE_WORKFLOW_REPORT_ONLY_DOWNLOADER_RC:-0}"
  CITADEL_RC="${MORTGAGE_WORKFLOW_REPORT_ONLY_CITADEL_RC:-0}"
  SPLITS_RC="${MORTGAGE_WORKFLOW_REPORT_ONLY_SPLITS_RC:-0}"
  TOKENOMICS_RC="${MORTGAGE_WORKFLOW_REPORT_ONLY_TOKENOMICS_RC:-0}"
  COVERAGE_RC="${MORTGAGE_WORKFLOW_REPORT_ONLY_COVERAGE_RC:-0}"
  CITADEL_PREFLIGHT_RC="${MORTGAGE_WORKFLOW_REPORT_ONLY_CITADEL_PREFLIGHT_RC:-0}"
  CITADEL_HAR_DIAGNOSTICS_RC="${MORTGAGE_WORKFLOW_REPORT_ONLY_CITADEL_HAR_DIAGNOSTICS_RC:-0}"
  echo "[mortgage-workflow] Report-only refresh; skipping live diagnostics, downloads, and splits; workbook writes remain guarded unless explicitly enabled"
  run_report_only_citadel_har_diagnostics
  configure_downloader_auto_profile_from_citadel_har_diagnostics
  run_report_only_citadel_har_replay_downloader
  run_report_only_mortgage_downloader
  MORTGAGE_ARCHIVE_RESTORE_APPLY=0 run_archive_restore
  run_report_only_mortgage_splits_diagnostic
  run_report_only_no_mortgage_financials_guard
  run_report_only_tokenomics_diagnostic
  run_report_only_downloader_coverage
  run_report_only_tokenomics_diagnostic
  run_evidence_advance
  run_evidence_refresh
  run_capture_queue
  run_capture_queue_processor
  run_replayability_gate
  if [ "$DRY_RUN" = "1" ]; then
    TOKENOMICS_WRITE_GUARD_STATUS="dry_run_skipped"
    TOKENOMICS_WRITE_GUARD_REASON="dry-run-no-tokenomics-workbook-write"
    echo "[mortgage-workflow] Report-only dry-run: skipping Co-Ownership Mortgage Tokenomics workbook writes" >&2
  elif evaluate_tokenomics_write_guard; then
    if [ "${MORTGAGE_WORKFLOW_REPORT_ONLY_APPLY_TOKENOMICS:-0}" = "1" ]; then
      if [ -x "$TOKENOMICS_SCRIPT" ] && "$PY" -c 'import openpyxl, pypdf' >/dev/null 2>&1; then
        echo "[mortgage-workflow] Report-only: applying idempotent tokenomics workbook cells from current statement/PMT evidence..."
        REPORT_ONLY_TOKENOMICS_WRITE_LIVE=1
        set +e
        "${MORTGAGE_WORKFLOW_STEP_TIMEOUT_ARGS[@]}" "$PY" "$TOKENOMICS_SCRIPT" --month "$MORTGAGE_STATEMENT_TARGET_MONTH" --coverage-report "$MORTGAGE_COVERAGE_REPORT" --report "$TOKENOMICS_REPORT"
        TOKENOMICS_RC=$?
        set -e
        if [ "$TOKENOMICS_RC" -ne 0 ]; then
          TOKENOMICS_WRITE_GUARD_STATUS="failed_after_ready"
          TOKENOMICS_WRITE_GUARD_REASON="tokenomics-update-failed"
          echo "[mortgage-workflow] Report-only tokenomics update returned rc=$TOKENOMICS_RC" >&2
        fi
      else
        TOKENOMICS_WRITE_GUARD_STATUS="blocked"
        TOKENOMICS_WRITE_GUARD_REASON="tokenomics-dependencies-missing"
        TOKENOMICS_RC=1
        echo "[mortgage-workflow] Report-only: skipping tokenomics update; script or dependencies missing" >&2
      fi
    else
      echo "[mortgage-workflow] Report-only: tokenomics workbook write skipped; set MORTGAGE_WORKFLOW_REPORT_ONLY_APPLY_TOKENOMICS=1 to apply idempotent cells"
    fi
  else
    if [ "${MORTGAGE_WORKFLOW_REPORT_ONLY_APPLY_TOKENOMICS:-0}" = "1" ] && [ "${COOWNERSHIP_MORTGAGE_TOKENOMICS_APPLY_READY_PROPERTIES:-1}" = "1" ]; then
      if [ -x "$TOKENOMICS_SCRIPT" ] && "$PY" -c 'import openpyxl, pypdf' >/dev/null 2>&1; then
        echo "[mortgage-workflow] Report-only: applying verified ready-property tokenomics cells while keeping full gate in review..."
        REPORT_ONLY_TOKENOMICS_WRITE_LIVE=1
        set +e
        "${MORTGAGE_WORKFLOW_STEP_TIMEOUT_ARGS[@]}" "$PY" "$TOKENOMICS_SCRIPT" --month "$MORTGAGE_STATEMENT_TARGET_MONTH" --coverage-report "$MORTGAGE_COVERAGE_REPORT" --report "$TOKENOMICS_REPORT" --apply-ready-properties
        TOKENOMICS_RC=$?
        set -e
        if [ "$TOKENOMICS_RC" -ne 0 ]; then
          echo "[mortgage-workflow] Report-only ready-property tokenomics apply returned rc=$TOKENOMICS_RC; outstanding blockers remain in review" >&2
        fi
      fi
    elif [ "${COOWNERSHIP_MORTGAGE_TOKENOMICS_APPLY_READY_PROPERTIES:-1}" = "1" ]; then
      echo "[mortgage-workflow] Report-only: ready-property tokenomics workbook write skipped; set MORTGAGE_WORKFLOW_REPORT_ONLY_APPLY_TOKENOMICS=1 to apply idempotent cells"
    fi
  fi
  write_gate_report \
    "${MORTGAGE_WORKFLOW_REPORT_ONLY_STATUS:-review}" \
    "${MORTGAGE_WORKFLOW_REPORT_ONLY_REASON:-report-only-refresh}" \
    "$DOWNLOADER_RC" \
    "$CITADEL_RC" \
    "$SPLITS_RC" \
    "$TOKENOMICS_RC" \
    "$COVERAGE_RC"
  exit 0
fi

if [ "$LAST" = "$STAMP" ]; then
  echo "[mortgage-workflow] Already completed for ${STAMP}, skipping"
  write_gate_report "ok" "already-completed" "$DOWNLOADER_RC" "$CITADEL_RC" "$SPLITS_RC" "$TOKENOMICS_RC" "$COVERAGE_RC"
  exit 0
fi

# --- Diagnostic: inspect the supplied Citadel HAR before live auth-dependent work ---
if [ -x "$CITADEL_HAR_DIAGNOSTICS_SCRIPT" ]; then
  echo "[mortgage-workflow] Running Citadel HAR workflow diagnostic..."
  set +e
  CITADEL_HAR_DIAGNOSTICS_REPORT="$CITADEL_HAR_DIAGNOSTICS_REPORT" \
  CITADEL_CAPTURE_PLAN_REPORT="$CITADEL_CAPTURE_PLAN_REPORT" \
  CITADEL_CAPTURE_PLAN_MARKDOWN="$CITADEL_CAPTURE_PLAN_MARKDOWN" \
  CITADEL_TARGET_MONTH="${CITADEL_TARGET_MONTH:-$DOWNLOADER_CITADEL_TARGET_MONTH}" \
  "${MORTGAGE_WORKFLOW_STEP_TIMEOUT_ARGS[@]}" "$NODE" "$CITADEL_HAR_DIAGNOSTICS_SCRIPT" > "$CITADEL_HAR_DIAGNOSTICS_REPORT.stdout" 2>"$CITADEL_HAR_DIAGNOSTICS_REPORT.stderr"
  CITADEL_HAR_DIAGNOSTICS_RC=$?
  set -e
  if [ "$CITADEL_HAR_DIAGNOSTICS_RC" -ne 0 ]; then
    echo "[mortgage-workflow] Citadel HAR diagnostic returned rc=$CITADEL_HAR_DIAGNOSTICS_RC; continuing to auth preflight" >&2
  fi
  if [ -f "$CITADEL_HAR_DIAGNOSTICS_REPORT" ]; then
    echo "[mortgage-workflow] Citadel HAR diagnostic is workflow evidence only; skipping token replay"
  fi
else
  echo "[mortgage-workflow] Citadel HAR diagnostic script missing or not executable, skipping diagnostic"
fi
configure_downloader_auto_profile_from_citadel_har_diagnostics

# --- Diagnostic: capture current Citadel browser auth state without downloading ---
if [ -x "$CITADEL_AUTH_PREFLIGHT_SCRIPT" ]; then
  echo "[mortgage-workflow] Running Citadel auth preflight diagnostic..."
  set +e
  CITADEL_AUTH_PREFLIGHT_REPORT="$CITADEL_AUTH_PREFLIGHT_REPORT" \
  "${MORTGAGE_WORKFLOW_STEP_TIMEOUT_ARGS[@]}" "$NODE" "$CITADEL_AUTH_PREFLIGHT_SCRIPT" > "$CITADEL_AUTH_PREFLIGHT_REPORT.stdout" 2>"$CITADEL_AUTH_PREFLIGHT_REPORT.stderr"
  CITADEL_PREFLIGHT_RC=$?
  set -e
  if [ "$CITADEL_PREFLIGHT_RC" -ne 0 ]; then
    echo "[mortgage-workflow] Citadel auth preflight returned rc=$CITADEL_PREFLIGHT_RC; continuing to downloader" >&2
  fi
else
  echo "[mortgage-workflow] Citadel auth preflight script missing or not executable, skipping diagnostic"
fi

# --- Step 1: Download mortgage statements from co-owner-paid servicers ---
if [ -x "$MORTGAGE_DOWNLOADER_RUNNER" ]; then
  echo "[mortgage-workflow] Running mortgage statement downloader registry..."
  DOWNLOADER_PROFILE="${MORTGAGE_DOWNLOADER_PROFILE:-$DOWNLOADER_AUTO_PROFILE}"
  DOWNLOADER_PROFILE_ARGS=()
  if [ -n "$DOWNLOADER_PROFILE" ]; then
    DOWNLOADER_PROFILE_ARGS=(--profile "$DOWNLOADER_PROFILE")
  fi
  downloader_timeout_args=()
  if command -v timeout >/dev/null 2>&1 && [ "$MORTGAGE_DOWNLOADER_WORKFLOW_TIMEOUT_SECONDS" != "0" ]; then
    downloader_timeout_args=(timeout --kill-after=15s "${MORTGAGE_DOWNLOADER_WORKFLOW_TIMEOUT_SECONDS}s")
  fi
  set +e
  MORTGAGE_DOWNLOADER_REPORT="$MORTGAGE_DOWNLOADER_REPORT" \
  MORTGAGE_DOWNLOADER_PROFILE="$DOWNLOADER_PROFILE" \
  MORTGAGE_DOWNLOADER_ALLOW_HAR_MODE="${MORTGAGE_DOWNLOADER_ALLOW_HAR_MODE:-$DOWNLOADER_ALLOW_HAR_MODE}" \
  MORTGAGE_DOWNLOADER_FALLBACK_ON_PREPARE_FAIL="${MORTGAGE_DOWNLOADER_FALLBACK_ON_PREPARE_FAIL:-$DOWNLOADER_FALLBACK_ON_PREPARE_FAIL}" \
  MORTGAGE_STATEMENT_TARGET_MONTH="$MORTGAGE_STATEMENT_TARGET_MONTH" \
  BASELANE_MORTGAGE_STATEMENT_TARGET_MONTH="$MORTGAGE_STATEMENT_TARGET_MONTH" \
  CITADEL_TARGET_MONTH="${CITADEL_TARGET_MONTH:-$DOWNLOADER_CITADEL_TARGET_MONTH}" \
  MORTGAGE_DOWNLOADER_CITADEL_TARGET_MONTH="$DOWNLOADER_CITADEL_TARGET_MONTH" \
  "${downloader_timeout_args[@]}" "$PY" "$MORTGAGE_DOWNLOADER_RUNNER" "${DOWNLOADER_PROFILE_ARGS[@]}" --target-month "$MORTGAGE_STATEMENT_TARGET_MONTH" --report "$MORTGAGE_DOWNLOADER_REPORT" > "$MORTGAGE_DOWNLOADER_REPORT.stdout" 2>"$MORTGAGE_DOWNLOADER_REPORT.stderr"
  DOWNLOADER_RC=$?
  set -e
  if [ "$DOWNLOADER_RC" -eq 124 ]; then
    CITADEL_RC=124
    "$PY" - "$MORTGAGE_DOWNLOADER_REPORT" "$MORTGAGE_DOWNLOADER_WORKFLOW_TIMEOUT_SECONDS" "$MORTGAGE_STATEMENT_TARGET_MONTH" <<'PY'
import json
import sys
from datetime import datetime, timezone

path, timeout_seconds, target_month = sys.argv[1:]
payload = {
    "job": "mortgage_statement_downloaders",
    "status": "timeout",
    "rc": 124,
    "timeout_seconds": int(timeout_seconds),
    "error": "mortgage downloader registry timed out",
    "target_month": target_month,
    "mortgage_statement_target_month": target_month,
    "citadel_download_rc": 124,
    "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY
  elif [ -f "$MORTGAGE_DOWNLOADER_REPORT" ]; then
    CITADEL_RC="$("$PY" - "$MORTGAGE_DOWNLOADER_REPORT" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
    print(int(data.get("citadel_download_rc") or 0))
except Exception:
    print(2)
PY
)"
  else
    CITADEL_RC="$DOWNLOADER_RC"
  fi
  if [ "$DOWNLOADER_RC" -ne 0 ]; then
    echo "[mortgage-workflow] Mortgage downloader registry returned rc=$DOWNLOADER_RC; continuing to splits" >&2
  fi
else
  echo "[mortgage-workflow] Mortgage downloader runner missing or not executable, skipping downloads"
fi

# --- Step 2: Restore current-month archive statement candidates idempotently ---
run_archive_restore

# --- Step 3: Baselane native mortgage splits ---
# The splits script is safe in --json (read-only diagnostic). The non-json
# interactive path queries unsplit Citadel transactions and prompts before
# applying. In the automated cron context we run the diagnostic first; if
# unsplit transactions are found, the diagnostic report surfaces them for
# the weekly review packet. Automated application requires operator approval.
if [ -x "$MORTGAGE_SPLITS_SCRIPT" ]; then
  echo "[mortgage-workflow] Running Baselane mortgage split diagnostic for ${STAMP}..."
  set +e
  "${MORTGAGE_WORKFLOW_STEP_TIMEOUT_ARGS[@]}" "$PY" "$MORTGAGE_SPLITS_SCRIPT" --json > "$MORTGAGE_SPLITS_REPORT" 2>/dev/null
  SPLITS_RC=$?
  set -e
  if [ "$SPLITS_RC" -ne 0 ]; then
    echo "[mortgage-workflow] Mortgage split diagnostic returned rc=$SPLITS_RC; review required" >&2
  fi
else
  echo "[mortgage-workflow] Mortgage splits script missing or not executable, skipping"
fi

# --- Step 4: Refresh tokenomics evidence in read-only mode before write guard ---
run_live_tokenomics_diagnostic

# --- Step 5: Audit downloader coverage for all co-owner-paid mortgage properties ---
if [ -x "$MORTGAGE_COVERAGE_SCRIPT" ]; then
  if "$PY" -c 'import openpyxl, pypdf' >/dev/null 2>&1; then
    echo "[mortgage-workflow] Auditing co-owner-paid mortgage downloader coverage..."
    set +e
    "${MORTGAGE_WORKFLOW_STEP_TIMEOUT_ARGS[@]}" "$PY" "$MORTGAGE_COVERAGE_SCRIPT" --month "$MORTGAGE_STATEMENT_TARGET_MONTH" --report "$MORTGAGE_COVERAGE_REPORT" > "$MORTGAGE_COVERAGE_REPORT.stdout" 2>"$MORTGAGE_COVERAGE_REPORT.stderr"
    COVERAGE_RC=$?
    set -e
    if [ "$COVERAGE_RC" -ne 0 ]; then
      echo "[mortgage-workflow] Downloader coverage audit returned rc=$COVERAGE_RC; surfacing in review packet" >&2
    fi
  else
    echo "[mortgage-workflow] Skipping downloader coverage audit: openpyxl/pypdf not installed" >&2
  fi
else
  echo "[mortgage-workflow] Downloader coverage audit script missing or not executable, skipping"
fi

# --- Step 6: Refresh tokenomics again with the freshly generated coverage report ---
run_live_tokenomics_diagnostic

# --- Step 7: Refresh workflow HAR evidence analyses and scaffold manifests offline ---
run_evidence_advance
run_evidence_refresh

# --- Step 8: Refresh idempotent visible-browser HAR capture queue ---
run_capture_queue

# --- Step 9: Process ready captured HARs idempotently ---
run_capture_queue_processor

# --- Step 10: Roll up coverage + HAR replayability evidence into one offline gate ---
run_replayability_gate

# --- Step 11: Update Co-Ownership Mortgage Tokenomics workbook only after guards pass ---
if [ "$DRY_RUN" = "1" ]; then
  TOKENOMICS_WRITE_GUARD_STATUS="dry_run_skipped"
  TOKENOMICS_WRITE_GUARD_REASON="dry-run-no-tokenomics-workbook-write"
  echo "[mortgage-workflow] Dry-run: skipping Co-Ownership Mortgage Tokenomics workbook writes" >&2
elif evaluate_tokenomics_write_guard; then
  if [ -x "$TOKENOMICS_SCRIPT" ]; then
    if "$PY" -c 'import openpyxl, pypdf' >/dev/null 2>&1; then
      echo "[mortgage-workflow] Updating Co-Ownership Mortgage Tokenomics workbook..."
      set +e
      "${MORTGAGE_WORKFLOW_STEP_TIMEOUT_ARGS[@]}" "$PY" "$TOKENOMICS_SCRIPT" --month "$MORTGAGE_STATEMENT_TARGET_MONTH" --coverage-report "$MORTGAGE_COVERAGE_REPORT" --report "$TOKENOMICS_REPORT"
      TOKENOMICS_RC=$?
      set -e
      if [ "$TOKENOMICS_RC" -ne 0 ]; then
        TOKENOMICS_WRITE_GUARD_STATUS="failed_after_ready"
        TOKENOMICS_WRITE_GUARD_REASON="tokenomics-update-failed"
        echo "[mortgage-workflow] Tokenomics update returned rc=$TOKENOMICS_RC" >&2
      fi
    else
      TOKENOMICS_WRITE_GUARD_STATUS="blocked"
      TOKENOMICS_WRITE_GUARD_REASON="tokenomics-dependencies-missing"
      TOKENOMICS_RC=1
      echo "[mortgage-workflow] Skipping tokenomics update: openpyxl/pypdf not installed" >&2
    fi
  else
    TOKENOMICS_WRITE_GUARD_STATUS="blocked"
    TOKENOMICS_WRITE_GUARD_REASON="tokenomics-script-missing"
    TOKENOMICS_RC=1
    echo "[mortgage-workflow] Tokenomics script missing or not executable, skipping"
  fi
else
  echo "[mortgage-workflow] Blocking Co-Ownership Mortgage Tokenomics workbook write: $TOKENOMICS_WRITE_GUARD_REASON" >&2
  # Keep the full workbook gate in review, but still write cells for properties
  # whose current-month evidence is complete. Set the env var to 0 to disable.
  if [ "${COOWNERSHIP_MORTGAGE_TOKENOMICS_APPLY_READY_PROPERTIES:-1}" = "1" ]; then
    if [ -x "$TOKENOMICS_SCRIPT" ] && "$PY" -c 'import openpyxl, pypdf' >/dev/null 2>&1; then
      echo "[mortgage-workflow] Applying verified ready-property tokenomics cells while keeping full gate in review..."
      set +e
      "${MORTGAGE_WORKFLOW_STEP_TIMEOUT_ARGS[@]}" "$PY" "$TOKENOMICS_SCRIPT" --month "$MORTGAGE_STATEMENT_TARGET_MONTH" --coverage-report "$MORTGAGE_COVERAGE_REPORT" --report "$TOKENOMICS_REPORT" --apply-ready-properties
      TOKENOMICS_RC=$?
      set -e
      if [ "$TOKENOMICS_RC" -ne 0 ]; then
        echo "[mortgage-workflow] Ready-property tokenomics apply returned rc=$TOKENOMICS_RC; outstanding blockers remain in review" >&2
      fi
    fi
  fi
fi

review_reason() {
  local reasons=()
  if [ "$TOKENOMICS_WRITE_GUARD_STATUS" = "blocked" ]; then
    reasons+=("tokenomics-workbook-write-blocked")
  fi
  if [ "$DOWNLOADER_RC" -ne 0 ]; then
    reasons+=("mortgage-downloader-failed")
  fi
  if [ "$CITADEL_RC" -ne 0 ]; then
    reasons+=("citadel-download-failed")
  fi
  if [ "$SPLITS_RC" -ne 0 ]; then
    reasons+=("mortgage-splits-failed")
  fi
  if [ "$MORTGAGE_ARCHIVE_RESTORE_RC" -ne 0 ]; then
    reasons+=("mortgage-archive-restore-failed")
  fi
  if [ "$TOKENOMICS_RC" -ne 0 ]; then
    reasons+=("tokenomics-update-failed")
  fi
  if [ "$MORTGAGE_REPLAYABILITY_RC" -ne 0 ]; then
    reasons+=("mortgage-replayability-review")
  fi
  if [ "${MORTGAGE_EVIDENCE_ADVANCE_RC:-0}" -ne 0 ]; then
    reasons+=("mortgage-evidence-advance-review")
  fi
  if [ "${MORTGAGE_EVIDENCE_REFRESH_RC:-0}" -ne 0 ]; then
    reasons+=("mortgage-evidence-refresh-review")
  fi
  local joined=""
  local reason
  for reason in "${reasons[@]}"; do
    if [ -n "$joined" ]; then
      joined="${joined};${reason}"
    else
      joined="$reason"
    fi
  done
  printf '%s\n' "${joined:-unknown}"
}

review_rc() {
  if [ "$DOWNLOADER_RC" -ne 0 ]; then
    printf '%s\n' "$DOWNLOADER_RC"
  elif [ "$CITADEL_RC" -ne 0 ]; then
    printf '%s\n' "$CITADEL_RC"
  elif [ "$SPLITS_RC" -ne 0 ]; then
    printf '%s\n' "$SPLITS_RC"
  elif [ "$MORTGAGE_ARCHIVE_RESTORE_RC" -ne 0 ]; then
    printf '%s\n' "$MORTGAGE_ARCHIVE_RESTORE_RC"
  elif [ "$TOKENOMICS_RC" -ne 0 ]; then
    printf '%s\n' "$TOKENOMICS_RC"
  elif [ "$MORTGAGE_REPLAYABILITY_RC" -ne 0 ]; then
    printf '%s\n' "$MORTGAGE_REPLAYABILITY_RC"
  elif [ "${MORTGAGE_EVIDENCE_ADVANCE_RC:-0}" -ne 0 ]; then
    printf '%s\n' "$MORTGAGE_EVIDENCE_ADVANCE_RC"
  else
    printf '1\n'
  fi
}

# Mark complete only if every required mortgage workflow stage passed.
if [ "$DOWNLOADER_RC" -eq 0 ] && [ "$CITADEL_RC" -eq 0 ] && [ "$SPLITS_RC" -eq 0 ] && [ "$MORTGAGE_ARCHIVE_RESTORE_RC" -eq 0 ] && [ "$TOKENOMICS_RC" -eq 0 ] && [ "${MORTGAGE_EVIDENCE_ADVANCE_RC:-0}" -eq 0 ] && [ "$MORTGAGE_REPLAYABILITY_RC" -eq 0 ]; then
  echo "$STAMP" > "$STATE_FILE"
  write_gate_report "ok" "completed" "$DOWNLOADER_RC" "$CITADEL_RC" "$SPLITS_RC" "$TOKENOMICS_RC" "$COVERAGE_RC"
  echo "[mortgage-workflow] Completed for ${STAMP}"
else
  REASON="$(review_reason)"
  write_gate_report "review" "$REASON" "$DOWNLOADER_RC" "$CITADEL_RC" "$SPLITS_RC" "$TOKENOMICS_RC" "$COVERAGE_RC"
  echo "[mortgage-workflow] Review required for ${STAMP}: ${REASON}; will retry next run" >&2
  exit "$(review_rc)"
fi
