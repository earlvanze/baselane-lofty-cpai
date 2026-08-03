#!/usr/bin/env node
const fs = require('fs');
const path = require('path');

const OPENCLAW_ROOT = process.env.OPENCLAW_ROOT || path.join(process.env.HOME || '', '.openclaw');
const ROOT = process.env.WORKSPACE_ROOT || path.join(OPENCLAW_ROOT, 'workspace');
const REPORTS = path.join(ROOT, 'reports');
const HAR_PATH = process.env.CITADEL_HAR_PATH || '/mnt/f/har/citadel_loansphereservicingdigital.bkiconnect.com.har';
const REPORT_PATH = process.env.CITADEL_HAR_DIAGNOSTICS_REPORT || path.join(REPORTS, 'citadel_har_workflow_diagnostics_report.json');
const CAPTURE_PLAN_REPORT = process.env.CITADEL_CAPTURE_PLAN_REPORT || path.join(REPORTS, 'citadel_capture_plan.json');
const CAPTURE_PLAN_MARKDOWN = process.env.CITADEL_CAPTURE_PLAN_MARKDOWN || path.join(REPORTS, 'citadel_capture_plan.md');
const BW_RECONCILE_REPORT = process.env.CITADEL_BW_RECONCILE_REPORT || path.join(REPORTS, 'citadel_bw_password_reconcile_report.json');
const LOAN_ORG = process.env.CITADEL_LOAN_ORG || '414';
const LOAN_ID = process.env.CITADEL_LOAN_ID || '1910008671';
const CAPTURE_PRECONDITION_PREPARE_COMMAND = 'CITADEL_CAPTURE_PREPARE_ONLY=1 scripts/citadel_manual_capture_har.sh';

function currentTargetMonth() {
  const configured = String(
    process.env.CITADEL_TARGET_MONTH
    || process.env.MORTGAGE_STATEMENT_TARGET_MONTH
    || process.env.BASELANE_MORTGAGE_STATEMENT_TARGET_MONTH
    || process.env.BASELANE_MONTHLY_TARGET_STAMP
    || ''
  ).trim();
  if (/^20\d{2}-\d{2}$/.test(configured)) return configured;
  return offsetYearMonth(new Date().toISOString().slice(0, 7), targetMonthDefaultOffset());
}

function targetMonthDefaultOffset() {
  const configured = String(
    process.env.CITADEL_TARGET_MONTH_DEFAULT_OFFSET
    || process.env.CITADEL_STATEMENT_MONTH_OFFSET
    || '-1'
  ).trim();
  return /^-?\d+$/.test(configured) ? Number(configured) : -1;
}

function offsetYearMonth(yearMonth, offset) {
  const match = /^(20\d{2})-(\d{2})$/.exec(String(yearMonth || ''));
  if (!match) return new Date().toISOString().slice(0, 7);
  const date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1 + Number(offset || 0), 1));
  return date.toISOString().slice(0, 7);
}

function shellQuote(value) {
  const text = String(value ?? '');
  if (text === '') return "''";
  return `'${text.replace(/'/g, `'\\''`)}'`;
}

function harReplayDownloaderCommand(harPath = HAR_PATH) {
  return [
    'MORTGAGE_DOWNLOADER_ALLOW_HAR_MODE=1',
    'MORTGAGE_DOWNLOADER_PROFILE=har_replay',
    'CITADEL_LOGIN_MODE=har',
    `CITADEL_HAR_PATH=${shellQuote(harPath)}`,
    'scripts/run_mortgage_statement_downloaders.py',
  ].join(' ');
}

function buildPostCaptureDiagnosticsCommand(captureHarPath) {
  const captureDiagnosticsReport = path.join(REPORTS, 'citadel_replay_capture_diagnostics_report.json');
  const capturePlanReport = path.join(REPORTS, 'citadel_replay_capture_plan.json');
  const capturePlanMarkdown = path.join(REPORTS, 'citadel_replay_capture_plan.md');
  return [
    `CITADEL_HAR_PATH=${shellQuote(captureHarPath)}`,
    `CITADEL_HAR_DIAGNOSTICS_REPORT=${shellQuote(captureDiagnosticsReport)}`,
    `CITADEL_CAPTURE_PLAN_REPORT=${shellQuote(capturePlanReport)}`,
    `CITADEL_CAPTURE_PLAN_MARKDOWN=${shellQuote(capturePlanMarkdown)}`,
    'node scripts/citadel_har_workflow_diagnostics.js',
  ].join(' ');
}

function buildCaptureWrapperCommand(captureHarPath, captureReportPath, extraEnv) {
  const env = Object.entries(extraEnv || {}).map(([key, value]) => `${key}=${shellQuote(value)}`);
  return [
    ...env,
    `CITADEL_CAPTURE_HAR_PATH=${shellQuote(captureHarPath)}`,
    `CITADEL_CAPTURE_REPORT=${shellQuote(captureReportPath)}`,
    'scripts/citadel_manual_capture_then_download.sh',
  ].join(' ');
}

function buildAdvanceWorkflowEvidenceCommands(captureHarPath, targetMonth) {
  const reportPath = path.join(REPORTS, 'mortgage_workflow_evidence_advance_90-madison-ave.json');
  const base = [
    'python3 scripts/advance_mortgage_workflow_evidence.py',
    `--property ${shellQuote('90 Madison Ave')}`,
    `--har ${shellQuote(`90 Madison Ave=${captureHarPath}`)}`,
    `--intake ${shellQuote(path.join(ROOT, 'config', 'mortgage_downloader_intake.json'))}`,
    `--registry ${shellQuote(path.join(ROOT, 'config', 'mortgage_statement_downloaders.json'))}`,
    `--report-dir ${shellQuote(REPORTS)}`,
    `--report ${shellQuote(reportPath)}`,
    `--target-month ${shellQuote(targetMonth)}`,
    '--write-stubs',
    '--install-registry-entries',
  ].join(' ');
  return {
    report_path: reportPath,
    command: base,
    apply_command: `${base} --apply-evidence --apply-registry`,
  };
}

function buildVerifiedCaptureInstallCommands(captureHarPath, captureReportPath) {
  const reportPath = path.join(REPORTS, 'citadel_verified_capture_install_report.json');
  const canonicalHarPath = process.env.CITADEL_CANONICAL_HAR_PATH || HAR_PATH;
  const base = [
    'python3 scripts/install_verified_citadel_capture_har.py',
    `--capture-har ${shellQuote(captureHarPath)}`,
    `--capture-report ${shellQuote(captureReportPath)}`,
    `--canonical-har ${shellQuote(canonicalHarPath)}`,
    `--report ${shellQuote(reportPath)}`,
  ].join(' ');
  return {
    canonical_har_path: canonicalHarPath,
    report_path: reportPath,
    command: base,
    apply_command: `${base} --apply`,
    wrapper_command: buildCaptureWrapperCommand(captureHarPath, captureReportPath, {
      CITADEL_CAPTURE_SKIP_CAPTURE: '1',
      CITADEL_CAPTURE_INSTALL_VERIFIED_HAR: '1',
      CITADEL_CAPTURE_INSTALL_VERIFIED_HAR_ONLY: '1',
      CITADEL_CAPTURE_INSTALL_VERIFIED_HAR_REPORT: reportPath,
      CITADEL_CANONICAL_HAR_PATH: canonicalHarPath,
    }),
    wrapper_apply_command: buildCaptureWrapperCommand(captureHarPath, captureReportPath, {
      CITADEL_CAPTURE_SKIP_CAPTURE: '1',
      CITADEL_CAPTURE_INSTALL_VERIFIED_HAR: '1',
      CITADEL_CAPTURE_INSTALL_VERIFIED_HAR_APPLY: '1',
      CITADEL_CAPTURE_INSTALL_VERIFIED_HAR_ONLY: '1',
      CITADEL_CAPTURE_INSTALL_VERIFIED_HAR_REPORT: reportPath,
      CITADEL_CANONICAL_HAR_PATH: canonicalHarPath,
    }),
  };
}

function buildPostCaptureCheckInfo() {
  const scriptPath = path.join(REPORTS, 'mortgage_workflow_evidence_check_90-madison-ave.sh');
  return {
    script_path: scriptPath,
    command: `bash ${shellQuote(scriptPath)}`,
    safe_to_run_after_har_capture: true,
  };
}

function withoutTopLevelKeys(value, keys) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return value;
  const clone = { ...value };
  for (const key of keys) delete clone[key];
  return clone;
}

function preserveVolatileFieldsWhenUnchanged(nextValue, existingPath, keys) {
  let existing = null;
  try {
    existing = JSON.parse(fs.readFileSync(existingPath, 'utf8'));
  } catch {
    return nextValue;
  }
  const nextStable = JSON.stringify(withoutTopLevelKeys(nextValue, keys));
  const existingStable = JSON.stringify(withoutTopLevelKeys(existing, keys));
  if (nextStable !== existingStable) return nextValue;
  for (const key of keys) {
    if (typeof existing[key] === 'string' && existing[key].trim()) nextValue[key] = existing[key];
  }
  return nextValue;
}

function latestLiveAuthSummary() {
  let candidates = [];
  try {
    candidates = fs.readdirSync(REPORTS)
      .filter(name => /^citadel_live_login_attempt_.*\.json$/.test(name))
      .map(name => path.join(REPORTS, name));
  } catch {
    return {};
  }
  if (!candidates.length) return {};
  candidates.sort((a, b) => {
    try {
      const delta = fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs;
      if (delta) return delta;
    } catch {}
    return path.basename(b).localeCompare(path.basename(a));
  });
  const latest = candidates[0];
  let data = null;
  try {
    data = JSON.parse(fs.readFileSync(latest, 'utf8'));
  } catch (error) {
    return {
      latest_live_auth_report: latest,
      latest_live_auth_status: 'unreadable',
      latest_live_auth_error: String(error && error.message || error).slice(0, 180),
    };
  }
  const summary = {
    latest_live_auth_report: latest,
    latest_live_auth_status: data.status || null,
  };
  for (const key of [
    'started_at',
    'ended_at',
    'login_mode',
    'tab_source',
    'credentials_available',
    'auth_failure_reason',
    'auth_failure_visible_reason',
    'credential_state_drift_suspected',
    'login_form_last_result',
    'login_form_submitted',
    'manual_auth_required',
    'manual_auth_reason',
    'manual_auth_portal_url',
    'manual_auth_file',
    'oauth_password_grant_failure_count',
    'oauth_password_grant_error_codes',
    'otp_wait_ms',
    'otp_gws_enabled',
    'otp_gws_last_status',
    'otp_gws_otp_written',
  ]) {
    if (Object.prototype.hasOwnProperty.call(data, key)) summary[`latest_live_auth_${key}`] = data[key];
  }
  const directAuth = data.direct_auth && typeof data.direct_auth === 'object' ? data.direct_auth : {};
  for (const key of [
    'status',
    'first_error',
    'no_mfa_handoff_after_password',
    'no_mfa_handoff_reason',
    'password_token_request_shape_matches_har_success',
    'password_token_request_structure_matches_har_initial_mfa_challenge',
    'otp_send_via_type',
    'otp_request_status',
    'otp_request_shape_matches_har_success',
    'mfa_request_uuid_source',
    'mfa_request_uuid_generated',
  ]) {
    if (Object.prototype.hasOwnProperty.call(directAuth, key)) summary[`latest_live_auth_direct_${key}`] = directAuth[key];
  }
  if (Object.prototype.hasOwnProperty.call(directAuth, 'status')) {
    summary.latest_live_auth_direct_auth_status = directAuth.status;
  }
  summary.latest_live_auth_blocker = data.auth_failure_reason || data.manual_auth_reason || (
    data.status && data.status !== 'ok' ? data.status : null
  );
  return summary;
}

function latestCredentialReconcileSummary() {
  let data = null;
  try {
    data = JSON.parse(fs.readFileSync(BW_RECONCILE_REPORT, 'utf8'));
  } catch {
    return {
      latest_credential_reconcile_report: BW_RECONCILE_REPORT,
      latest_credential_reconcile_status: null,
    };
  }
  const summary = {
    latest_credential_reconcile_report: BW_RECONCILE_REPORT,
    latest_credential_reconcile_status: data.status || null,
  };
  for (const key of [
    'reason',
    'successful_password_request_found',
    'bw_session_status',
    'bw_item_found',
    'bw_item_name',
    'bw_item_uri_host_match',
    'username_matches_har',
    'password_matched_before_update',
    'password_updated',
    'apply_update',
  ]) {
    if (Object.prototype.hasOwnProperty.call(data, key)) summary[`latest_credential_reconcile_${key}`] = data[key];
  }
  return summary;
}

function credentialVerifiedAgainstSuccessfulHar(reconcile) {
  return reconcile.latest_credential_reconcile_status === 'ok'
    && reconcile.latest_credential_reconcile_successful_password_request_found === true
    && reconcile.latest_credential_reconcile_username_matches_har === true
    && reconcile.latest_credential_reconcile_password_matched_before_update === true;
}

function capturePreconditionSummary(liveAuth, captureRequired, reconcile = {}) {
  if (!captureRequired) {
    return {
      capture_precondition_status: 'not_required',
      capture_precondition_blocker: null,
      capture_precondition_reason: null,
      capture_precondition_manual_auth_required: false,
      capture_precondition_next_action: null,
      capture_precondition_safe_to_capture_now: true,
      capture_precondition_prepare_command: null,
      capture_precondition_safe_to_prepare_now: false,
    };
  }
  const status = liveAuth.latest_live_auth_status || '';
  const blocker = liveAuth.latest_live_auth_blocker || '';
  const visibleReason = liveAuth.latest_live_auth_auth_failure_visible_reason || '';
  const authReason = liveAuth.latest_live_auth_auth_failure_reason || liveAuth.latest_live_auth_manual_auth_reason || '';
  const directAuthStatus = liveAuth.latest_live_auth_direct_status || '';
  const directOtpShapeMatchesHar = liveAuth.latest_live_auth_direct_otp_request_shape_matches_har_success === true;
  const manualRequired = liveAuth.latest_live_auth_manual_auth_required === true;
  if (!status) {
    return {
      capture_precondition_status: 'auth_not_verified',
      capture_precondition_blocker: 'latest_live_auth_missing',
      capture_precondition_reason: 'no_latest_live_auth_attempt_report',
      capture_precondition_manual_auth_required: true,
      capture_precondition_next_action: 'run_live_auth_check_before_capture',
      capture_precondition_safe_to_capture_now: false,
      capture_precondition_prepare_command: CAPTURE_PRECONDITION_PREPARE_COMMAND,
      capture_precondition_safe_to_prepare_now: true,
    };
  }
  if (status === 'unreadable') {
    return {
      capture_precondition_status: 'auth_status_unreadable',
      capture_precondition_blocker: 'latest_live_auth_report_unreadable',
      capture_precondition_reason: liveAuth.latest_live_auth_error || null,
      capture_precondition_manual_auth_required: true,
      capture_precondition_next_action: 'repair_latest_live_auth_report_before_capture',
      capture_precondition_safe_to_capture_now: false,
      capture_precondition_prepare_command: CAPTURE_PRECONDITION_PREPARE_COMMAND,
      capture_precondition_safe_to_prepare_now: true,
    };
  }
  if (status === 'auth_failed' || blocker === 'credential_rejected_before_mfa' || blocker === 'mfa_source_unavailable') {
    if ((blocker === 'otp_request_failed' || authReason === 'otp_request_failed' || directAuthStatus === 'otp_request_failed') && directOtpShapeMatchesHar) {
      return {
        capture_precondition_status: 'blocked_by_retryable_portal_auth',
        capture_precondition_blocker: 'otp_request_matches_successful_har_but_portal_rejected',
        capture_precondition_reason: 'live_otp_request_shape_matched_successful_har_but_portal_returned_unauthorized',
        capture_precondition_manual_auth_required: manualRequired,
        capture_precondition_next_action: 'retry_visible_citadel_auth_or_check_portal_status_before_capture',
        capture_precondition_safe_to_capture_now: false,
        capture_precondition_prepare_command: CAPTURE_PRECONDITION_PREPARE_COMMAND,
        capture_precondition_safe_to_prepare_now: true,
      };
    }
    if ((blocker === 'credential_rejected_before_mfa' || authReason === 'credential_rejected_before_mfa') && credentialVerifiedAgainstSuccessfulHar(reconcile)) {
      return {
        capture_precondition_status: 'blocked_by_retryable_portal_auth',
        capture_precondition_blocker: 'credential_verified_but_portal_rejected_before_mfa',
        capture_precondition_reason: 'credential_verified_against_successful_har_but_portal_rejected_before_mfa',
        capture_precondition_manual_auth_required: manualRequired,
        capture_precondition_next_action: 'retry_visible_citadel_auth_or_check_portal_status_before_capture',
        capture_precondition_safe_to_capture_now: false,
        capture_precondition_prepare_command: CAPTURE_PRECONDITION_PREPARE_COMMAND,
        capture_precondition_safe_to_prepare_now: true,
      };
    }
    return {
      capture_precondition_status: 'blocked_by_live_auth',
      capture_precondition_blocker: blocker || status,
      capture_precondition_reason: visibleReason || authReason || blocker || status,
      capture_precondition_manual_auth_required: manualRequired,
      capture_precondition_next_action: 'repair_citadel_portal_login_before_capture',
      capture_precondition_safe_to_capture_now: false,
      capture_precondition_prepare_command: CAPTURE_PRECONDITION_PREPARE_COMMAND,
      capture_precondition_safe_to_prepare_now: true,
    };
  }
  if (status === 'auth_required' || blocker === 'auth_required' || manualRequired) {
    return {
      capture_precondition_status: 'blocked_by_manual_auth',
      capture_precondition_blocker: blocker || status,
      capture_precondition_reason: visibleReason || authReason || blocker || status,
      capture_precondition_manual_auth_required: true,
      capture_precondition_next_action: 'complete_visible_citadel_auth_before_capture',
      capture_precondition_safe_to_capture_now: false,
      capture_precondition_prepare_command: CAPTURE_PRECONDITION_PREPARE_COMMAND,
      capture_precondition_safe_to_prepare_now: true,
    };
  }
  if (status === 'ok') {
    return {
      capture_precondition_status: 'ready',
      capture_precondition_blocker: null,
      capture_precondition_reason: null,
      capture_precondition_manual_auth_required: false,
      capture_precondition_next_action: 'run_visible_capture',
      capture_precondition_safe_to_capture_now: true,
      capture_precondition_prepare_command: CAPTURE_PRECONDITION_PREPARE_COMMAND,
      capture_precondition_safe_to_prepare_now: true,
    };
  }
  return {
    capture_precondition_status: 'live_auth_attempt_recorded',
    capture_precondition_blocker: null,
    capture_precondition_reason: status,
    capture_precondition_manual_auth_required: manualRequired,
    capture_precondition_next_action: 'inspect_latest_live_auth_before_capture',
    capture_precondition_safe_to_capture_now: false,
    capture_precondition_prepare_command: CAPTURE_PRECONDITION_PREPARE_COMMAND,
    capture_precondition_safe_to_prepare_now: true,
  };
}

function buildCapturePlan(diagnostics) {
  const nextAction = diagnostics.next_action || buildNextAction(diagnostics);
  const captureRequired = nextAction.capture_required !== false && diagnostics.status !== 'ok';
  const planStatus = captureRequired ? 'ready_for_visible_capture' : 'not_required';
  const captureHarPath = process.env.CITADEL_CAPTURE_HAR_PATH || path.join(REPORTS, 'citadel_replay_capture.har');
  const captureReportPath = process.env.CITADEL_CAPTURE_REPORT || path.join(REPORTS, 'citadel_replay_capture_report.json');
  const workflowEvidence = buildAdvanceWorkflowEvidenceCommands(captureHarPath, diagnostics.target_month || currentTargetMonth());
  const verifiedInstall = buildVerifiedCaptureInstallCommands(captureHarPath, captureReportPath);
  const postCaptureCheck = buildPostCaptureCheckInfo();
  const liveAuth = latestLiveAuthSummary();
  const credentialReconcile = latestCredentialReconcileSummary();
  return {
    job: 'citadel-visible-capture-plan',
    generated_at: new Date().toISOString(),
    status: planStatus,
    property: '90 Madison Ave',
    servicer: 'Citadel / LoanSphere BKI',
    diagnostics_report: REPORT_PATH,
    capture_plan_markdown: CAPTURE_PLAN_MARKDOWN,
    diagnostics_status: diagnostics.status,
    target_month: diagnostics.target_month,
    target_month_replayable_document_available: diagnostics.target_month_replayable_document_available,
    target_month_replayable_document_payload_count: diagnostics.target_month_replayable_document_payload_count,
    replayable_statement_months: diagnostics.replayable_statement_months || [],
    statement_document_months: diagnostics.statement_document_months || [],
    capture_quality_status: diagnostics.capture_quality_status,
    replay_blocker: diagnostics.replay_blocker,
    missing_response_body_count: diagnostics.missing_response_body_count,
    missing_response_body_paths: diagnostics.missing_response_body_paths || [],
    missing_response_body_path_counts: diagnostics.missing_response_body_path_counts || {},
    capture_required: captureRequired,
    capture_command: nextAction.capture_command || 'scripts/citadel_manual_capture_har.sh',
    next_command: nextAction.next_command || 'scripts/citadel_manual_capture_then_download.sh',
    capture_har_path: captureHarPath,
    capture_report: captureReportPath,
    canonical_har_path: verifiedInstall.canonical_har_path,
    post_capture_diagnostics_command: buildPostCaptureDiagnosticsCommand(captureHarPath),
    post_capture_check_script_path: postCaptureCheck.script_path,
    post_capture_check_command: postCaptureCheck.command,
    post_capture_check_safe_to_run_after_har_capture: postCaptureCheck.safe_to_run_after_har_capture,
    validate_existing_capture_command: buildCaptureWrapperCommand(captureHarPath, captureReportPath, {
      CITADEL_CAPTURE_SKIP_CAPTURE: '1',
      CITADEL_CAPTURE_VALIDATE_ONLY: '1',
    }),
    advance_capture_workflow_evidence_dry_run_command: buildCaptureWrapperCommand(captureHarPath, captureReportPath, {
      CITADEL_CAPTURE_SKIP_CAPTURE: '1',
      CITADEL_CAPTURE_ADVANCE_WORKFLOW_EVIDENCE: '1',
    }),
    advance_capture_workflow_evidence_apply_command: buildCaptureWrapperCommand(captureHarPath, captureReportPath, {
      CITADEL_CAPTURE_SKIP_CAPTURE: '1',
      CITADEL_CAPTURE_ADVANCE_WORKFLOW_EVIDENCE: '1',
      CITADEL_CAPTURE_APPLY_WORKFLOW_EVIDENCE: '1',
    }),
    install_verified_capture_report: verifiedInstall.report_path,
    install_verified_capture_dry_run_command: verifiedInstall.wrapper_command,
    install_verified_capture_apply_command: verifiedInstall.wrapper_apply_command,
    install_verified_capture_direct_dry_run_command: verifiedInstall.command,
    install_verified_capture_direct_apply_command: verifiedInstall.apply_command,
    workflow_evidence_advance_report: workflowEvidence.report_path,
    advance_workflow_evidence_command: workflowEvidence.command,
    advance_workflow_evidence_apply_command: workflowEvidence.apply_command,
    required_response_paths: nextAction.required_response_paths || diagnostics.missing_response_body_paths || [],
    required_response_path_counts: nextAction.required_response_path_counts || diagnostics.missing_response_body_path_counts || {},
    response_body_requirements: nextAction.response_body_requirements || diagnostics.response_body_requirements || [],
    source_direct_pdf_candidate_count: nextAction.source_direct_pdf_candidate_count || diagnostics.source_direct_pdf_candidate_count || 0,
    source_direct_pdf_path_counts: nextAction.source_direct_pdf_path_counts || diagnostics.source_direct_pdf_path_counts || {},
    source_required_response_candidate_count: nextAction.source_required_response_candidate_count || diagnostics.source_required_response_candidate_count || 0,
    source_required_response_path_counts: nextAction.source_required_response_path_counts || diagnostics.source_required_response_path_counts || {},
    response_body_requirement_progress: nextAction.response_body_requirement_progress || diagnostics.response_body_requirement_progress || [],
    source_direct_pdf_limit: Number(process.env.CITADEL_CAPTURE_SOURCE_DIRECT_PDF_LIMIT || 10),
    source_direct_pdf_filenames: nextAction.source_direct_pdf_filenames || diagnostics.source_direct_pdf_filenames || [],
    source_direct_pdf_filename_candidates: nextAction.source_direct_pdf_filename_candidates || diagnostics.source_direct_pdf_filename_candidates || [],
    target_month_direct_pdf_filenames: nextAction.target_month_direct_pdf_filenames || diagnostics.target_month_direct_pdf_filenames || [],
    target_month_direct_pdf_filename_candidates: nextAction.target_month_direct_pdf_filename_candidates || diagnostics.target_month_direct_pdf_filename_candidates || [],
    target_month_direct_pdf_body_missing_candidate_count: nextAction.target_month_direct_pdf_body_missing_candidate_count || diagnostics.target_month_direct_pdf_body_missing_candidate_count || 0,
    safe_to_run_automatically: false,
    manual_auth_required: true,
    ...liveAuth,
    ...credentialReconcile,
    ...capturePreconditionSummary(liveAuth, captureRequired, credentialReconcile),
    secrets_included: false,
    raw_source_urls_included: false,
    raw_query_values_included: false,
    notes: [
      'Use a visible authenticated browser session; do not run this as a headless portal login.',
      'The plan intentionally includes only sanitized response paths and counts; source HAR URLs and query values stay local runtime-only.',
    ],
  };
}

function attachCapturePlanToNextAction(diagnostics, plan) {
  if (!diagnostics || !plan) return diagnostics;
  const nextAction = diagnostics.next_action || buildNextAction(diagnostics);
  diagnostics.next_action = {
    ...nextAction,
    capture_plan_report: CAPTURE_PLAN_REPORT,
    capture_plan_markdown: plan.capture_plan_markdown,
    capture_har_path: plan.capture_har_path,
    capture_report: plan.capture_report,
    canonical_har_path: plan.canonical_har_path,
    post_capture_diagnostics_command: plan.post_capture_diagnostics_command,
    post_capture_check_script_path: plan.post_capture_check_script_path,
    post_capture_check_command: plan.post_capture_check_command,
    post_capture_check_safe_to_run_after_har_capture: plan.post_capture_check_safe_to_run_after_har_capture,
    validate_existing_capture_command: plan.validate_existing_capture_command,
    advance_capture_workflow_evidence_dry_run_command: plan.advance_capture_workflow_evidence_dry_run_command,
    advance_capture_workflow_evidence_apply_command: plan.advance_capture_workflow_evidence_apply_command,
    install_verified_capture_report: plan.install_verified_capture_report,
    install_verified_capture_dry_run_command: plan.install_verified_capture_dry_run_command,
    install_verified_capture_apply_command: plan.install_verified_capture_apply_command,
    install_verified_capture_direct_dry_run_command: plan.install_verified_capture_direct_dry_run_command,
    install_verified_capture_direct_apply_command: plan.install_verified_capture_direct_apply_command,
    workflow_evidence_advance_report: plan.workflow_evidence_advance_report,
    advance_workflow_evidence_command: plan.advance_workflow_evidence_command,
    advance_workflow_evidence_apply_command: plan.advance_workflow_evidence_apply_command,
    capture_precondition_status: plan.capture_precondition_status,
    capture_precondition_blocker: plan.capture_precondition_blocker,
    capture_precondition_reason: plan.capture_precondition_reason,
    capture_precondition_manual_auth_required: plan.capture_precondition_manual_auth_required,
    capture_precondition_next_action: plan.capture_precondition_next_action,
    capture_precondition_safe_to_capture_now: plan.capture_precondition_safe_to_capture_now,
    capture_precondition_prepare_command: plan.capture_precondition_prepare_command,
    capture_precondition_safe_to_prepare_now: plan.capture_precondition_safe_to_prepare_now,
  };
  diagnostics.next_command = diagnostics.next_action.next_command;
  return diagnostics;
}

function promoteCapturePlanSummary(diagnostics, plan) {
  if (!diagnostics || !plan) return diagnostics;
  const keys = [
    'latest_live_auth_report',
    'latest_live_auth_status',
    'latest_live_auth_blocker',
    'latest_live_auth_started_at',
    'latest_live_auth_ended_at',
    'latest_live_auth_login_mode',
    'latest_live_auth_tab_source',
    'latest_live_auth_credentials_available',
    'latest_live_auth_auth_failure_reason',
    'latest_live_auth_auth_failure_visible_reason',
    'latest_live_auth_manual_auth_required',
    'latest_live_auth_manual_auth_reason',
    'latest_live_auth_manual_auth_file',
    'latest_live_auth_oauth_password_grant_failure_count',
    'latest_live_auth_oauth_password_grant_error_codes',
    'latest_live_auth_direct_auth_status',
    'latest_live_auth_direct_status',
    'latest_live_auth_direct_first_error',
    'latest_live_auth_direct_no_mfa_handoff_after_password',
    'latest_live_auth_direct_no_mfa_handoff_reason',
    'latest_live_auth_direct_password_token_request_shape_matches_har_success',
    'latest_live_auth_direct_password_token_request_structure_matches_har_initial_mfa_challenge',
    'latest_live_auth_direct_otp_request_shape_matches_har_success',
    'latest_credential_reconcile_report',
    'latest_credential_reconcile_status',
    'latest_credential_reconcile_successful_password_request_found',
    'latest_credential_reconcile_username_matches_har',
    'latest_credential_reconcile_password_matched_before_update',
    'capture_precondition_status',
    'capture_precondition_blocker',
    'capture_precondition_reason',
    'capture_precondition_manual_auth_required',
    'capture_precondition_next_action',
    'capture_precondition_safe_to_capture_now',
    'capture_precondition_prepare_command',
    'capture_precondition_safe_to_prepare_now',
  ];
  for (const key of keys) {
    if (Object.prototype.hasOwnProperty.call(plan, key)) diagnostics[key] = plan[key];
  }
  return diagnostics;
}

function markdownValue(value) {
  if (value === undefined || value === null || value === '') return 'none';
  if (typeof value === 'string') return value;
  return JSON.stringify(value);
}

function markdownPathCounts(counts) {
  const entries = Object.entries(counts || {}).sort((a, b) => a[0].localeCompare(b[0]));
  if (!entries.length) return ['- none'];
  return entries.map(([safePath, count]) => `- \`${safePath}\`: ${count}`);
}

function markdownRequirements(requirements) {
  if (!Array.isArray(requirements) || requirements.length === 0) return ['- none'];
  return requirements.map(item => {
    const safePath = item && item.path ? item.path : 'unknown';
    const role = item && item.role ? item.role : 'workflow_response';
    const missingCount = item && item.missing_response_body_count !== undefined ? item.missing_response_body_count : 0;
    const directPdfCount = item && item.direct_pdf_missing_response_count !== undefined ? item.direct_pdf_missing_response_count : 0;
    const quality = item && item.required_capture_quality ? item.required_capture_quality : 'full_response_body';
    return `- \`${safePath}\` role=\`${role}\` missing=${missingCount} direct_pdf_missing=${directPdfCount} quality=\`${quality}\``;
  });
}

function markdownRequirementProgress(progress) {
  if (!Array.isArray(progress) || progress.length === 0) return ['- none'];
  return progress.map(item => {
    const safePath = item && item.path ? item.path : 'unknown';
    const role = item && item.role ? item.role : 'workflow_response';
    const requiredCount = item && item.required_count !== undefined ? item.required_count : 0;
    const capturedCount = item && item.captured_count !== undefined ? item.captured_count : 0;
    const missingCount = item && item.missing_count !== undefined ? item.missing_count : 0;
    const sourceCandidateCount = item && item.source_candidate_count !== undefined ? item.source_candidate_count : 0;
    return `- \`${safePath}\` role=\`${role}\` required=${requiredCount} captured=${capturedCount} missing=${missingCount} source_candidates=${sourceCandidateCount}`;
  });
}

function markdownFilenameCandidates(candidates) {
  if (!Array.isArray(candidates) || candidates.length === 0) return ['- none'];
  return candidates.map(item => {
    const filename = item && item.filename ? item.filename : 'unknown';
    const safePath = item && item.path ? item.path : 'unknown';
    const month = item && item.statement_month ? item.statement_month : 'unknown';
    const missing = item && item.body_missing !== undefined ? item.body_missing : false;
    const sourceEntry = item && item.source_entry_index !== undefined ? item.source_entry_index : 'unknown';
    const method = item && item.method ? item.method : 'unknown';
    const status = item && item.status !== undefined ? item.status : 'unknown';
    const contentSize = item && item.content_size !== undefined ? item.content_size : 'unknown';
    const responseBodySize = item && item.response_body_size !== undefined ? item.response_body_size : 'unknown';
    return `- \`${filename}\` entry=\`${sourceEntry}\` method=\`${method}\` status=\`${status}\` month=\`${month}\` path=\`${safePath}\` content_size=\`${contentSize}\` response_body_size=\`${responseBodySize}\` body_missing=\`${missing}\``;
  });
}

function buildCapturePlanMarkdown(plan) {
  return [
    '# Citadel Visible Capture Checklist',
    '',
    `- property: \`${markdownValue(plan.property)}\``,
    `- servicer: \`${markdownValue(plan.servicer)}\``,
    `- status: \`${markdownValue(plan.status)}\``,
    `- diagnostics_status: \`${markdownValue(plan.diagnostics_status)}\``,
    `- target_month: \`${markdownValue(plan.target_month)}\``,
    `- target_month_replayable_document_available: \`${markdownValue(plan.target_month_replayable_document_available)}\``,
    `- target_month_replayable_document_payload_count: \`${markdownValue(plan.target_month_replayable_document_payload_count)}\``,
    `- replayable_statement_months: \`${markdownValue(plan.replayable_statement_months)}\``,
    `- statement_document_months: \`${markdownValue(plan.statement_document_months)}\``,
    `- capture_quality_status: \`${markdownValue(plan.capture_quality_status)}\``,
    `- replay_blocker: \`${markdownValue(plan.replay_blocker)}\``,
    `- capture_required: \`${markdownValue(plan.capture_required)}\``,
    `- diagnostics_report: \`${markdownValue(plan.diagnostics_report)}\``,
    `- capture_plan_report: \`${markdownValue(CAPTURE_PLAN_REPORT)}\``,
    `- capture_har_path: \`${markdownValue(plan.capture_har_path)}\``,
    `- capture_report: \`${markdownValue(plan.capture_report)}\``,
    `- canonical_har_path: \`${markdownValue(plan.canonical_har_path)}\``,
    `- capture_quality_status: \`${markdownValue(plan.capture_quality_status)}\``,
    `- replay_blocker: \`${markdownValue(plan.replay_blocker)}\``,
    `- missing_response_body_count: \`${markdownValue(plan.missing_response_body_count)}\``,
    `- missing_response_body_path_counts: \`${markdownValue(plan.missing_response_body_path_counts)}\``,
    `- post_capture_check_script_path: \`${markdownValue(plan.post_capture_check_script_path)}\``,
    '',
    '## Latest Live Auth Attempt',
    '',
    `- live_auth_report: \`${markdownValue(plan.latest_live_auth_report)}\``,
    `- live_auth_status: \`${markdownValue(plan.latest_live_auth_status)}\``,
    `- live_auth_blocker: \`${markdownValue(plan.latest_live_auth_blocker)}\``,
    `- live_auth_failure_reason: \`${markdownValue(plan.latest_live_auth_auth_failure_reason)}\``,
    `- live_auth_failure_visible_reason: \`${markdownValue(plan.latest_live_auth_auth_failure_visible_reason)}\``,
    `- live_auth_manual_auth_required: \`${markdownValue(plan.latest_live_auth_manual_auth_required)}\``,
    `- live_auth_manual_auth_file: \`${markdownValue(plan.latest_live_auth_manual_auth_file)}\``,
    `- live_auth_oauth_error_codes: \`${markdownValue(plan.latest_live_auth_oauth_password_grant_error_codes)}\``,
    '',
    '## Credential Reconcile',
    '',
    `- credential_reconcile_report: \`${markdownValue(plan.latest_credential_reconcile_report)}\``,
    `- credential_reconcile_status: \`${markdownValue(plan.latest_credential_reconcile_status)}\``,
    `- credential_reconcile_successful_password_request_found: \`${markdownValue(plan.latest_credential_reconcile_successful_password_request_found)}\``,
    `- credential_reconcile_username_matches_har: \`${markdownValue(plan.latest_credential_reconcile_username_matches_har)}\``,
    `- credential_reconcile_password_matched_before_update: \`${markdownValue(plan.latest_credential_reconcile_password_matched_before_update)}\``,
    `- credential_reconcile_password_updated: \`${markdownValue(plan.latest_credential_reconcile_password_updated)}\``,
    '',
    '## Capture Precondition',
    '',
    `- capture_precondition_status: \`${markdownValue(plan.capture_precondition_status)}\``,
    `- capture_precondition_blocker: \`${markdownValue(plan.capture_precondition_blocker)}\``,
    `- capture_precondition_reason: \`${markdownValue(plan.capture_precondition_reason)}\``,
    `- capture_precondition_manual_auth_required: \`${markdownValue(plan.capture_precondition_manual_auth_required)}\``,
    `- capture_precondition_next_action: \`${markdownValue(plan.capture_precondition_next_action)}\``,
    `- capture_precondition_safe_to_capture_now: \`${markdownValue(plan.capture_precondition_safe_to_capture_now)}\``,
    `- capture_precondition_prepare_command: \`${markdownValue(plan.capture_precondition_prepare_command)}\``,
    `- capture_precondition_safe_to_prepare_now: \`${markdownValue(plan.capture_precondition_safe_to_prepare_now)}\``,
    '',
    '## Commands',
    '',
    `- capture: \`${markdownValue(plan.capture_command)}\``,
    `- capture_then_download: \`${markdownValue(plan.next_command)}\``,
    `- post_capture_diagnostics: \`${markdownValue(plan.post_capture_diagnostics_command)}\``,
    `- post_capture_check: \`${markdownValue(plan.post_capture_check_command)}\``,
    `- post_capture_check_safe_to_run_after_har_capture: \`${markdownValue(plan.post_capture_check_safe_to_run_after_har_capture)}\``,
    `- validate_existing_capture: \`${markdownValue(plan.validate_existing_capture_command)}\``,
    `- advance_capture_workflow_evidence_dry_run: \`${markdownValue(plan.advance_capture_workflow_evidence_dry_run_command)}\``,
    `- advance_capture_workflow_evidence_apply: \`${markdownValue(plan.advance_capture_workflow_evidence_apply_command)}\``,
    `- install_verified_capture_dry_run: \`${markdownValue(plan.install_verified_capture_dry_run_command)}\``,
    `- install_verified_capture_apply: \`${markdownValue(plan.install_verified_capture_apply_command)}\``,
    `- install_verified_capture_direct_dry_run: \`${markdownValue(plan.install_verified_capture_direct_dry_run_command)}\``,
    `- install_verified_capture_direct_apply: \`${markdownValue(plan.install_verified_capture_direct_apply_command)}\``,
    `- advance_workflow_evidence: \`${markdownValue(plan.advance_workflow_evidence_command)}\``,
    `- advance_workflow_evidence_apply: \`${markdownValue(plan.advance_workflow_evidence_apply_command)}\``,
    '',
    '## After Successful Capture',
    '',
    '- Run `post_capture_diagnostics` against the captured HAR path above.',
    '- Run `post_capture_check` to verify workflow endpoint/body coverage without using live credentials.',
    '- Run `validate_existing_capture` to prove the captured HAR passes the replay gate without advancing evidence or downloading statements.',
    '- Run `install_verified_capture_dry_run` to confirm the replayable capture can replace the canonical HAR without writing or replaying statements.',
    '- Run `install_verified_capture_apply` only after the dry-run says it is ready or already installed.',
    '- Run `advance_capture_workflow_evidence_dry_run` to reuse the captured HAR, advance workflow evidence in dry-run mode, and then replay the downloader.',
    '- Run `advance_capture_workflow_evidence_apply` only after the dry-run output is acceptable.',
    '- Run `advance_workflow_evidence` only after diagnostics shows the required response bodies and target-month statement payload are present.',
    '- Run `advance_workflow_evidence_apply` only after the dry-run advance report is acceptable.',
    '',
    '## Required Sanitized Response Paths',
    '',
    ...markdownPathCounts(plan.required_response_path_counts),
    '',
    '## Response Body Requirements',
    '',
    ...markdownRequirements(plan.response_body_requirements),
    '',
    '## Response Body Capture Progress',
    '',
    ...markdownRequirementProgress(plan.response_body_requirement_progress),
    '',
    '## Source Candidate Counts',
    '',
    `- source_direct_pdf_candidate_count: \`${markdownValue(plan.source_direct_pdf_candidate_count)}\``,
    ...markdownPathCounts(plan.source_direct_pdf_path_counts),
    `- source_direct_pdf_filenames: \`${markdownValue(plan.source_direct_pdf_filenames)}\``,
    `- target_month_direct_pdf_filenames: \`${markdownValue(plan.target_month_direct_pdf_filenames)}\``,
    `- target_month_direct_pdf_body_missing_candidate_count: \`${markdownValue(plan.target_month_direct_pdf_body_missing_candidate_count)}\``,
    '',
    '## Target Month Direct PDF Filename Candidates',
    '',
    ...markdownFilenameCandidates(plan.target_month_direct_pdf_filename_candidates),
    `- source_required_response_candidate_count: \`${markdownValue(plan.source_required_response_candidate_count)}\``,
    ...markdownPathCounts(plan.source_required_response_path_counts),
    '',
    '## Safety',
    '',
    '- Use a visible authenticated browser session; do not run this as a headless portal login.',
    '- Do not replay HAR auth tokens; use the capture only as workflow evidence.',
    '- This checklist intentionally includes sanitized paths and counts only.',
    `- safe_to_run_automatically: \`${markdownValue(plan.safe_to_run_automatically)}\``,
    `- manual_auth_required: \`${markdownValue(plan.manual_auth_required)}\``,
    `- secrets_included: \`${markdownValue(plan.secrets_included)}\``,
    `- raw_source_urls_included: \`${markdownValue(plan.raw_source_urls_included)}\``,
    `- raw_query_values_included: \`${markdownValue(plan.raw_query_values_included)}\``,
    '',
    '## Success Criteria',
    '',
    '- Captured HAR has every required response body listed above.',
    '- Captured HAR includes a replayable statement payload for the target month.',
    '- Diagnostics report shows `missing_response_body_count` equal to `0`.',
    '- Diagnostics report shows `can_replay_documents` equal to `true` before downloader replay.',
    '',
  ].join('\n');
}

function buildNextAction(diagnostics) {
  if (diagnostics.can_replay_documents) {
    return {
      status: 'ready_to_replay',
      reason: null,
      next_command: harReplayDownloaderCommand(),
      capture_required: false,
    };
  }
  if (diagnostics.capture_quality_status === 'needs_full_response_bodies') {
    return {
      status: 'capture_full_response_bodies',
      reason: diagnostics.replay_blocker,
      next_command: 'scripts/citadel_manual_capture_then_download.sh',
      capture_command: 'scripts/citadel_manual_capture_har.sh',
      capture_required: true,
      target_month: diagnostics.target_month,
      target_month_replayable_document_available: diagnostics.target_month_replayable_document_available,
      target_month_replayable_document_payload_count: diagnostics.target_month_replayable_document_payload_count,
      replayable_statement_months: diagnostics.replayable_statement_months || [],
      statement_document_months: diagnostics.statement_document_months || [],
      required_response_paths: diagnostics.missing_response_body_paths,
      required_response_path_counts: diagnostics.missing_response_body_path_counts || {},
      response_body_requirements: diagnostics.response_body_requirements || [],
      response_body_requirement_progress: diagnostics.response_body_requirement_progress || [],
      source_direct_pdf_candidate_count: diagnostics.source_direct_pdf_candidate_count || 0,
      source_direct_pdf_path_counts: diagnostics.source_direct_pdf_path_counts || {},
      source_direct_pdf_filenames: diagnostics.source_direct_pdf_filenames || [],
      source_direct_pdf_filename_candidates: diagnostics.source_direct_pdf_filename_candidates || [],
      target_month_direct_pdf_filenames: diagnostics.target_month_direct_pdf_filenames || [],
      target_month_direct_pdf_filename_candidates: diagnostics.target_month_direct_pdf_filename_candidates || [],
      target_month_direct_pdf_body_missing_candidate_count: diagnostics.target_month_direct_pdf_body_missing_candidate_count || 0,
      source_required_response_candidate_count: diagnostics.source_required_response_candidate_count || 0,
      source_required_response_path_counts: diagnostics.source_required_response_path_counts || {},
    };
  }
  if (diagnostics.capture_quality_status === 'missing_target_month_statement') {
    return {
      status: 'capture_target_month_statement',
      reason: diagnostics.replay_blocker,
      next_command: 'scripts/citadel_manual_capture_then_download.sh',
      capture_command: 'scripts/citadel_manual_capture_har.sh',
      capture_required: true,
      target_month: diagnostics.target_month,
      target_month_replayable_document_available: diagnostics.target_month_replayable_document_available,
      target_month_replayable_document_payload_count: diagnostics.target_month_replayable_document_payload_count,
      replayable_statement_months: diagnostics.replayable_statement_months || [],
      statement_document_months: diagnostics.statement_document_months || [],
      required_response_paths: diagnostics.missing_response_body_paths || [],
      required_response_path_counts: diagnostics.missing_response_body_path_counts || {},
      response_body_requirements: diagnostics.response_body_requirements || [],
      response_body_requirement_progress: diagnostics.response_body_requirement_progress || [],
      source_direct_pdf_candidate_count: diagnostics.source_direct_pdf_candidate_count || 0,
      source_direct_pdf_path_counts: diagnostics.source_direct_pdf_path_counts || {},
      source_direct_pdf_filenames: diagnostics.source_direct_pdf_filenames || [],
      source_direct_pdf_filename_candidates: diagnostics.source_direct_pdf_filename_candidates || [],
      target_month_direct_pdf_filenames: diagnostics.target_month_direct_pdf_filenames || [],
      target_month_direct_pdf_filename_candidates: diagnostics.target_month_direct_pdf_filename_candidates || [],
      target_month_direct_pdf_body_missing_candidate_count: diagnostics.target_month_direct_pdf_body_missing_candidate_count || 0,
      source_required_response_candidate_count: diagnostics.source_required_response_candidate_count || 0,
      source_required_response_path_counts: diagnostics.source_required_response_path_counts || {},
    };
  }
  if (diagnostics.capture_quality_status === 'missing_har') {
    return {
      status: 'provide_workflow_har',
      reason: diagnostics.replay_blocker,
      next_command: 'scripts/citadel_manual_capture_har.sh',
      capture_required: true,
      target_month: diagnostics.target_month,
      target_month_replayable_document_available: diagnostics.target_month_replayable_document_available,
      target_month_replayable_document_payload_count: diagnostics.target_month_replayable_document_payload_count,
      replayable_statement_months: diagnostics.replayable_statement_months || [],
      statement_document_months: diagnostics.statement_document_months || [],
      required_response_paths: [],
      required_response_path_counts: {},
      response_body_requirement_progress: diagnostics.response_body_requirement_progress || [],
      source_direct_pdf_candidate_count: diagnostics.source_direct_pdf_candidate_count || 0,
      source_direct_pdf_path_counts: diagnostics.source_direct_pdf_path_counts || {},
      source_direct_pdf_filenames: diagnostics.source_direct_pdf_filenames || [],
      source_direct_pdf_filename_candidates: diagnostics.source_direct_pdf_filename_candidates || [],
      target_month_direct_pdf_filenames: diagnostics.target_month_direct_pdf_filenames || [],
      target_month_direct_pdf_filename_candidates: diagnostics.target_month_direct_pdf_filename_candidates || [],
      target_month_direct_pdf_body_missing_candidate_count: diagnostics.target_month_direct_pdf_body_missing_candidate_count || 0,
      source_required_response_candidate_count: diagnostics.source_required_response_candidate_count || 0,
      source_required_response_path_counts: diagnostics.source_required_response_path_counts || {},
    };
  }
  return {
    status: 'inspect_har_workflow',
    reason: diagnostics.replay_blocker,
    next_command: 'scripts/citadel_manual_capture_then_download.sh',
    capture_required: true,
    target_month: diagnostics.target_month,
    target_month_replayable_document_available: diagnostics.target_month_replayable_document_available,
    target_month_replayable_document_payload_count: diagnostics.target_month_replayable_document_payload_count,
    replayable_statement_months: diagnostics.replayable_statement_months || [],
    statement_document_months: diagnostics.statement_document_months || [],
    required_response_paths: diagnostics.missing_response_body_paths || [],
    required_response_path_counts: diagnostics.missing_response_body_path_counts || {},
    response_body_requirement_progress: diagnostics.response_body_requirement_progress || [],
    source_direct_pdf_candidate_count: diagnostics.source_direct_pdf_candidate_count || 0,
    source_direct_pdf_path_counts: diagnostics.source_direct_pdf_path_counts || {},
    source_direct_pdf_filenames: diagnostics.source_direct_pdf_filenames || [],
    source_direct_pdf_filename_candidates: diagnostics.source_direct_pdf_filename_candidates || [],
    target_month_direct_pdf_filenames: diagnostics.target_month_direct_pdf_filenames || [],
    target_month_direct_pdf_filename_candidates: diagnostics.target_month_direct_pdf_filename_candidates || [],
    target_month_direct_pdf_body_missing_candidate_count: diagnostics.target_month_direct_pdf_body_missing_candidate_count || 0,
    source_required_response_candidate_count: diagnostics.source_required_response_candidate_count || 0,
    source_required_response_path_counts: diagnostics.source_required_response_path_counts || {},
  };
}

function docField(doc, ...names) {
  for (const name of names) {
    const value = doc && doc[name];
    if (value !== undefined && value !== null && value !== '') return value;
  }
  return undefined;
}

function normalizeBase64Pdf(value) {
  if (!value) return null;
  const text = String(value).trim();
  const dataUrlMatch = /^data:application\/pdf;base64,(.+)$/i.exec(text);
  return dataUrlMatch ? dataUrlMatch[1] : text;
}

function pdfBase64FromJson(json) {
  return normalizeBase64Pdf(docField(
    json,
    'content',
    'Content',
    'base64',
    'Base64',
    'data',
    'Data',
    'DocumentContent',
    'documentContent',
    'document_content',
    'pdf',
    'Pdf',
    'PDF',
    'pdfContent',
    'PdfContent',
    'PDFContent',
    'fileContent',
    'FileContent',
  ));
}

function coalesce(...values) {
  return values.find(v => v !== undefined && v !== null && String(v).trim() !== '');
}

function unwrapArray(value, keys) {
  if (Array.isArray(value)) return value;
  if (!value || typeof value !== 'object') return [];
  for (const key of keys) {
    const child = value[key];
    if (Array.isArray(child)) return child;
  }
  return [];
}

function statementDateFromDoc(docName, docDate) {
  const candidates = [docDate, docName].filter(Boolean).map(String);
  for (const candidate of candidates) {
    let m = /(20\d{2})[-/](\d{2})[-/](\d{2})/.exec(candidate);
    if (m) return `${m[1]}-${m[2]}-${m[3]}`;
    m = /(\d{2})[-/](\d{2})[-/](20\d{2})/.exec(candidate);
    if (m) return `${m[3]}-${m[1]}-${m[2]}`;
    m = /(?:billing|mortgage)?statement[-_\s]+(\d{2})[-_\s]+(20\d{2})/i.exec(candidate);
    if (m) return `${m[2]}-${m[1]}-01`;
  }
  return '';
}

function statementMonthFromDoc(doc) {
  const docName = coalesce(
    doc && doc.name,
    doc && doc.Name,
    doc && doc.documentName,
    doc && doc.DocumentName,
    doc && doc.title,
    doc && doc.Title,
    doc && doc.type,
    doc && doc.Type,
    doc && doc.DocumentType,
    doc && doc.Description,
  ) || '';
  const docDate = coalesce(
    doc && doc.date,
    doc && doc.Date,
    doc && doc.statementDate,
    doc && doc.StatementDate,
    doc && doc.createdDate,
    doc && doc.CreatedDate,
    doc && doc.postedDate,
    doc && doc.PostedDate,
    doc && doc.DocumentDate,
    doc && doc.documentDate,
    doc && doc.DocumentCreationDate,
    doc && doc.documentCreationDate,
    doc && doc.creationDate,
    doc && doc.CreationDate,
  ) || '';
  const date = statementDateFromDoc(docName, docDate);
  return date ? date.slice(0, 7) : '';
}

function isStatementDocument(doc) {
  const name = String(coalesce(
    doc && doc.name,
    doc && doc.Name,
    doc && doc.documentName,
    doc && doc.DocumentName,
    doc && doc.title,
    doc && doc.Title,
    doc && doc.type,
    doc && doc.Type,
    doc && doc.DocumentType,
    doc && doc.Description,
  ) || '').toLowerCase();
  return name.includes('statement') || name.includes('billing') || name.includes('mortgage');
}

function addUnique(list, value) {
  if (value && !list.includes(value)) list.push(value);
}

function isPdfMime(value) {
  return String(value || '').toLowerCase().split(';')[0].trim() === 'application/pdf';
}

function isJsonMime(value) {
  const mime = String(value || '').toLowerCase().split(';')[0].trim();
  return mime === 'application/json' || mime.endsWith('+json');
}

function isSuccessfulResponse(response) {
  const status = Number(response && response.status || 0);
  return status >= 200 && status < 300;
}

function textLooksHtml(value) {
  const text = String(value || '');
  return /^\s*</.test(text) || /<!doctype\s+html|<html[\s>]/i.test(text);
}

function headerValue(headers, targetName) {
  const needle = String(targetName || '').toLowerCase();
  if (!needle) return '';
  for (const header of headers || []) {
    if (String(header && header.name || '').toLowerCase() === needle) return String(header && header.value || '');
  }
  return '';
}

function sanitizeFilename(value) {
  const base = path.basename(String(value || '').replace(/\\/g, '/').trim());
  return base.replace(/[\x00-\x1F\x7F]/g, '').slice(0, 180);
}

function contentDispositionFilename(response) {
  const value = headerValue((response || {}).headers || [], 'content-disposition');
  if (!value) return '';
  const star = /filename\*\s*=\s*(?:UTF-8''|)([^;]+)/i.exec(value);
  if (star) {
    try {
      return sanitizeFilename(decodeURIComponent(star[1].trim().replace(/^"|"$/g, '')));
    } catch {
      return sanitizeFilename(star[1].trim().replace(/^"|"$/g, ''));
    }
  }
  const regular = /filename\s*=\s*("[^"]+"|[^;]+)/i.exec(value);
  return regular ? sanitizeFilename(regular[1].trim().replace(/^"|"$/g, '')) : '';
}

function missingBodyPath(url) {
  try {
    const parsed = new URL(url);
    const keys = Array.from(parsed.searchParams.keys()).filter(Boolean).sort();
    return keys.length ? `${parsed.pathname}?${keys.join('&')}` : parsed.pathname;
  } catch {
    return String(url || '');
  }
}

function harPdfBuffer(entry) {
  if (!isSuccessfulResponse(entry && entry.response)) return null;
  const content = entry && entry.response && entry.response.content || {};
  const text = content.text;
  if (!text || !isPdfMime(content.mimeType)) return null;
  const buffer = content.encoding === 'base64' ? Buffer.from(text, 'base64') : Buffer.from(String(text), 'binary');
  return buffer.length > 1000 && buffer.slice(0, 5).toString() === '%PDF-' ? buffer : null;
}

function pdfBufferFromBase64(value) {
  const pdfBase64 = normalizeBase64Pdf(value);
  if (!pdfBase64) return null;
  const buffer = Buffer.from(pdfBase64, 'base64');
  return buffer.length > 1000 && buffer.slice(0, 5).toString() === '%PDF-' ? buffer : null;
}

function harResponseText(entry) {
  const content = entry && entry.response && entry.response.content || {};
  const text = content.text;
  if (!text) return '';
  if (content.encoding === 'base64') {
    try {
      return Buffer.from(text, 'base64').toString('utf8');
    } catch {
      return '';
    }
  }
  return text;
}

function harJson(entry) {
  if (!isSuccessfulResponse(entry && entry.response)) return null;
  const content = entry && entry.response && entry.response.content || {};
  const text = harResponseText(entry);
  if (!text || textLooksHtml(text)) return null;
  if (content.mimeType && !isJsonMime(content.mimeType)) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function buildDiagnostics() {
  const diagnostics = {
    job: 'citadel-har-workflow-diagnostics',
    started_at: new Date().toISOString(),
    har_path: HAR_PATH,
    har_path_exists: !!(HAR_PATH && fs.existsSync(HAR_PATH)),
    target_month: currentTargetMonth(),
    status: 'unknown',
    capture_quality_status: 'unknown',
    replay_blocker: null,
    endpoint_statuses: [],
    embedded_response_body_count: 0,
    replayable_json_response_count: 0,
    replayable_document_payload_count: 0,
    replayable_statement_months: [],
    statement_document_months: [],
    target_month_replayable_document_payload_count: 0,
    target_month_replayable_document_available: false,
    direct_pdf_response_count: 0,
    source_direct_pdf_candidate_count: 0,
    source_direct_pdf_path_counts: {},
    source_direct_pdf_filenames: [],
    source_direct_pdf_filename_candidates: [],
    target_month_direct_pdf_filenames: [],
    target_month_direct_pdf_filename_candidates: [],
    target_month_direct_pdf_body_missing_candidate_count: 0,
    source_required_response_candidate_count: 0,
    source_required_response_path_counts: {},
    direct_pdf_missing_response_count: 0,
    direct_pdf_missing_response_paths: [],
    missing_response_body_count: 0,
    missing_response_body_paths: [],
    missing_response_body_path_counts: {},
    direct_pdf_missing_response_path_counts: {},
    invalid_response_body_count: 0,
    invalid_response_body_paths: [],
    invalid_response_body_path_counts: {},
    response_body_requirements: [],
    response_body_requirement_progress: [],
    embedded_access_token_count: 0,
    statement_list_body_available: false,
    document_list_body_available: false,
    document_detail_body_count: 0,
    document_detail_embedded_pdf_count: 0,
    can_replay_documents: false,
    errors: [],
  };
  if (!diagnostics.har_path_exists) {
    diagnostics.status = 'review';
    diagnostics.capture_quality_status = 'missing_har';
    diagnostics.replay_blocker = 'har_path_unavailable';
    diagnostics.next_action = buildNextAction(diagnostics);
    diagnostics.next_command = diagnostics.next_action.next_command;
    return diagnostics;
  }
  try {
    const har = JSON.parse(fs.readFileSync(HAR_PATH, 'utf8'));
    const entries = (((har || {}).log || {}).entries || []);
    diagnostics.entry_count = entries.length;
    for (let index = 0; index < entries.length; index++) {
      const entry = entries[index] || {};
      const request = entry.request || {};
      const response = entry.response || {};
      const content = response.content || {};
      const url = String(request.url || '');
      const directPdf = harPdfBuffer(entry);
      const isPotentialDirectPdf = isPdfMime(content.mimeType) || url.includes('/LISViewDoc.aspx');
      if (![
        '/oauth2/v3/token',
        '/sd/loan-info/v2/my/loans-summary',
        `/sd/loan-maint/v2/my/loans/${LOAN_ORG}/${LOAN_ID}/eStatements`,
        `/lsd/api/v1/my/loans/${LOAN_ORG}/${LOAN_ID}/docs`,
      ].some(fragment => url.includes(fragment)) && !directPdf && !isPotentialDirectPdf) {
        continue;
      }
      const text = harResponseText(entry);
      if (text) diagnostics.embedded_response_body_count += 1;
      const json = harJson(entry);
      if (json) diagnostics.replayable_json_response_count += 1;
      const pathName = (() => {
        try { return new URL(url).pathname; } catch { return url; }
      })();
      const isStatementList = pathName.includes(`/sd/loan-maint/v2/my/loans/${LOAN_ORG}/${LOAN_ID}/eStatements`);
      const isDocumentList = pathName.endsWith(`/lsd/api/v1/my/loans/${LOAN_ORG}/${LOAN_ID}/docs`);
      const isDocumentDetail = pathName.includes(`/lsd/api/v1/my/loans/${LOAN_ORG}/${LOAN_ID}/docs/`);
      const isDirectPdfResponse = isPdfMime(content.mimeType);
      if (json && (isStatementList || isDocumentList)) {
        const docs = unwrapArray(json, [
          'statements',
          'Statements',
          'eStatements',
          'EStatements',
          'Documents',
          'documents',
          'docs',
          'Docs',
          'data',
          'Data',
        ]);
        for (const doc of docs) {
          if (!isStatementDocument(doc)) continue;
          addUnique(diagnostics.statement_document_months, statementMonthFromDoc(doc));
        }
      }
      if (response.status === 200 && isPotentialDirectPdf) {
        const sourcePdfPath = missingBodyPath(url);
        diagnostics.source_direct_pdf_candidate_count += 1;
        diagnostics.source_direct_pdf_path_counts[sourcePdfPath] = (diagnostics.source_direct_pdf_path_counts[sourcePdfPath] || 0) + 1;
        const filename = contentDispositionFilename(response);
        if (filename) {
          const statementDate = statementDateFromDoc(filename, '');
          const statementMonth = statementDate ? statementDate.slice(0, 7) : '';
          const candidate = {
            filename,
            path: sourcePdfPath,
            source_entry_index: index,
            method: request.method || null,
            status: response.status,
            mime_type: content.mimeType || null,
            statement_month: statementMonth || null,
            content_size: Number(content.size || 0),
            response_body_size: Number(response.bodySize || 0),
            has_embedded_text: !!text,
            body_missing: Number(content.size || 0) > 0 && !text,
            replayable: !!directPdf,
          };
          if (text && !directPdf) candidate.invalid_body = true;
          addUnique(diagnostics.source_direct_pdf_filenames, filename);
          diagnostics.source_direct_pdf_filename_candidates.push(candidate);
          if (statementMonth && statementMonth === diagnostics.target_month) {
            addUnique(diagnostics.target_month_direct_pdf_filenames, filename);
            diagnostics.target_month_direct_pdf_filename_candidates.push(candidate);
            if (candidate.body_missing) diagnostics.target_month_direct_pdf_body_missing_candidate_count += 1;
          }
        }
      }
      if (isStatementList && json) diagnostics.statement_list_body_available = true;
      if (isDocumentList && json) diagnostics.document_list_body_available = true;
      if (isDocumentDetail && json) diagnostics.document_detail_body_count += 1;
      if (pathName.includes('/oauth2/v3/token') && json && (json.access_token || json.AccessToken)) {
        diagnostics.embedded_access_token_count += 1;
      }
      const hasMissingRelevantBody = (
        response.status === 200
        && (isStatementList || isDocumentList || isDocumentDetail || isDirectPdfResponse)
        && Number(content.size || 0) > 0
        && !text
      );
      const hasInvalidDirectPdfBody = (
        response.status === 200
        && isPotentialDirectPdf
        && !!text
        && !directPdf
      );
      const hasInvalidJsonWorkflowBody = (
        response.status === 200
        && (isStatementList || isDocumentList || isDocumentDetail)
        && !!text
        && !json
      );
      if (
        hasMissingRelevantBody
        || hasInvalidDirectPdfBody
        || hasInvalidJsonWorkflowBody
      ) {
        const bodyPath = missingBodyPath(url);
        diagnostics.missing_response_body_count += 1;
        diagnostics.missing_response_body_path_counts[bodyPath] = (diagnostics.missing_response_body_path_counts[bodyPath] || 0) + 1;
        if (!diagnostics.missing_response_body_paths.includes(bodyPath)) {
          diagnostics.missing_response_body_paths.push(bodyPath);
        }
        if (hasInvalidDirectPdfBody || hasInvalidJsonWorkflowBody) {
          diagnostics.invalid_response_body_count += 1;
          diagnostics.invalid_response_body_path_counts[bodyPath] = (diagnostics.invalid_response_body_path_counts[bodyPath] || 0) + 1;
          if (!diagnostics.invalid_response_body_paths.includes(bodyPath)) {
            diagnostics.invalid_response_body_paths.push(bodyPath);
          }
        }
        if (isDirectPdfResponse || hasInvalidDirectPdfBody) {
          diagnostics.direct_pdf_missing_response_count += 1;
          diagnostics.direct_pdf_missing_response_path_counts[bodyPath] = (diagnostics.direct_pdf_missing_response_path_counts[bodyPath] || 0) + 1;
          if (!diagnostics.direct_pdf_missing_response_paths.includes(bodyPath)) {
            diagnostics.direct_pdf_missing_response_paths.push(bodyPath);
          }
        }
      }
      diagnostics.endpoint_statuses.push({
        index,
        method: request.method,
        path: pathName,
        status: response.status,
        mime_type: content.mimeType,
        content_size: content.size,
        has_embedded_text: !!text,
        json_available: !!json,
      });
      if (json && pathName.includes('/docs/')) {
        const pdfBase64 = pdfBase64FromJson(json);
        if (pdfBufferFromBase64(pdfBase64)) {
          const statementMonth = isStatementDocument(json) ? statementMonthFromDoc(json) : '';
          diagnostics.replayable_document_payload_count += 1;
          diagnostics.document_detail_embedded_pdf_count += 1;
          addUnique(diagnostics.replayable_statement_months, statementMonth);
          if (statementMonth && statementMonth === diagnostics.target_month) {
            diagnostics.target_month_replayable_document_payload_count += 1;
          }
        }
      }
      if (directPdf) {
        diagnostics.replayable_document_payload_count += 1;
        diagnostics.direct_pdf_response_count += 1;
      }
    }
    if (
      diagnostics.direct_pdf_response_count > 0
      && diagnostics.statement_document_months.includes(diagnostics.target_month)
    ) {
      diagnostics.target_month_replayable_document_payload_count += diagnostics.direct_pdf_response_count;
      addUnique(diagnostics.replayable_statement_months, diagnostics.target_month);
    }
    diagnostics.target_month_replayable_document_available = diagnostics.target_month_replayable_document_payload_count > 0;
    diagnostics.response_body_requirements = buildResponseBodyRequirements(diagnostics);
    diagnostics.source_required_response_candidate_count = diagnostics.missing_response_body_count;
    diagnostics.source_required_response_path_counts = { ...diagnostics.missing_response_body_path_counts };
    diagnostics.response_body_requirement_progress = buildResponseBodyRequirementProgress(diagnostics);
    diagnostics.can_replay_documents = diagnostics.target_month_replayable_document_available;
    diagnostics.status = diagnostics.can_replay_documents ? 'ok' : 'review';
    if (diagnostics.can_replay_documents) {
      diagnostics.capture_quality_status = 'replayable';
    } else if (diagnostics.replayable_document_payload_count > 0) {
      diagnostics.capture_quality_status = 'missing_target_month_statement';
      diagnostics.replay_blocker = 'target_month_statement_unavailable';
    } else if (diagnostics.missing_response_body_count > 0) {
      diagnostics.capture_quality_status = 'needs_full_response_bodies';
      diagnostics.replay_blocker = 'missing_response_bodies';
    } else if (!diagnostics.statement_list_body_available && !diagnostics.document_list_body_available) {
      diagnostics.capture_quality_status = 'missing_statement_index';
      diagnostics.replay_blocker = 'no_statement_or_document_list_body';
    } else if (diagnostics.document_detail_body_count === 0) {
      diagnostics.capture_quality_status = 'missing_document_details';
      diagnostics.replay_blocker = 'no_document_detail_bodies';
    } else {
      diagnostics.capture_quality_status = 'no_embedded_pdf_payloads';
      diagnostics.replay_blocker = 'document_details_without_pdf_payloads';
    }
  } catch (error) {
    diagnostics.status = 'error';
    diagnostics.capture_quality_status = 'parse_error';
    diagnostics.replay_blocker = 'har_parse_error';
    diagnostics.errors.push(String(error && error.message || error).slice(0, 300));
  }
  diagnostics.next_action = buildNextAction(diagnostics);
  diagnostics.next_command = diagnostics.next_action.next_command;
  return diagnostics;
}

function responseBodyRequirementRole(safePath) {
  const path = String(safePath || '');
  if (path.includes('/LISViewDoc.aspx')) return 'direct_pdf';
  if (path.includes(`/sd/loan-maint/v2/my/loans/${LOAN_ORG}/${LOAN_ID}/eStatements`)) return 'statement_index';
  if (path.endsWith(`/lsd/api/v1/my/loans/${LOAN_ORG}/${LOAN_ID}/docs`)) return 'document_list';
  if (path.includes(`/lsd/api/v1/my/loans/${LOAN_ORG}/${LOAN_ID}/docs/`)) return 'document_detail';
  return 'workflow_response';
}

function buildResponseBodyRequirements(diagnostics) {
  const missingCounts = diagnostics.missing_response_body_path_counts || {};
  const directPdfCounts = diagnostics.direct_pdf_missing_response_path_counts || {};
  return Object.keys(missingCounts).sort().map(safePath => {
    const role = responseBodyRequirementRole(safePath);
    return {
      path: safePath,
      role,
      missing_response_body_count: missingCounts[safePath] || 0,
      direct_pdf_missing_response_count: directPdfCounts[safePath] || 0,
      required_capture_quality: 'full_response_body',
      notes: role === 'direct_pdf'
        ? 'Capture the PDF response body for this endpoint; query values stay omitted from reports.'
        : 'Capture the JSON response body for this endpoint.',
    };
  });
}

function buildResponseBodyRequirementProgress(diagnostics) {
  const requirements = diagnostics.response_body_requirements || [];
  const sourceCounts = diagnostics.source_required_response_path_counts || {};
  return requirements.map(item => {
    const safePath = item && item.path ? item.path : 'unknown';
    const missingCount = Number(item && item.missing_response_body_count || 0);
    return {
      path: safePath,
      role: item && item.role ? item.role : 'workflow_response',
      required_count: missingCount,
      captured_count: 0,
      missing_count: missingCount,
      satisfied: missingCount === 0,
      source_candidate_count: sourceCounts[safePath] || 0,
      required_capture_quality: item && item.required_capture_quality ? item.required_capture_quality : 'full_response_body',
    };
  });
}

fs.mkdirSync(path.dirname(REPORT_PATH), { recursive: true });
const diagnostics = buildDiagnostics();
diagnostics.ended_at = new Date().toISOString();
const capturePlan = buildCapturePlan(diagnostics);
attachCapturePlanToNextAction(diagnostics, capturePlan);
promoteCapturePlanSummary(diagnostics, capturePlan);
preserveVolatileFieldsWhenUnchanged(diagnostics, REPORT_PATH, ['started_at', 'ended_at']);
preserveVolatileFieldsWhenUnchanged(capturePlan, CAPTURE_PLAN_REPORT, ['generated_at']);
fs.writeFileSync(REPORT_PATH, JSON.stringify(diagnostics, null, 2));
fs.mkdirSync(path.dirname(CAPTURE_PLAN_REPORT), { recursive: true });
fs.writeFileSync(CAPTURE_PLAN_REPORT, JSON.stringify(capturePlan, null, 2));
fs.mkdirSync(path.dirname(CAPTURE_PLAN_MARKDOWN), { recursive: true });
fs.writeFileSync(CAPTURE_PLAN_MARKDOWN, buildCapturePlanMarkdown(capturePlan));
console.log(`[citadel-har] ${diagnostics.status} quality=${diagnostics.capture_quality_status} replayable=${diagnostics.replayable_document_payload_count} missing_bodies=${diagnostics.missing_response_body_count}`);
process.exitCode = diagnostics.status === 'ok' ? 0 : 1;
