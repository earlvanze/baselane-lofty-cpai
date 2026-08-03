#!/usr/bin/env node
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');
const { randomUUID } = require('crypto');

const OPENCLAW_ROOT = process.env.OPENCLAW_ROOT || path.join(process.env.HOME || '', '.openclaw');
const ROOT = process.env.WORKSPACE_ROOT || path.join(OPENCLAW_ROOT, 'workspace');
const REPORTS = path.join(ROOT, 'reports');
const VERSION_URL = process.env.CITADEL_CDP_VERSION_URL || process.env.BASELANE_CDP_VERSION_URL || 'http://127.0.0.1:9222/json/version';
const PORTAL_URL = 'https://loansphereservicingdigital.bkiconnect.com/mycitadelservicing/#/login';
const API_BASE = 'https://api.bkiconnect.com';
const LOAN_ORG = process.env.CITADEL_LOAN_ORG || '414';
const LOAN_ID = process.env.CITADEL_LOAN_ID || '1910008671';
const TIMEOUT_MS = Number(process.env.CITADEL_TIMEOUT_MS || 120000);
const AUTH_WAIT_MS = Number(process.env.CITADEL_AUTH_WAIT_MS || 120000);
const OTP_WAIT_MS = Number(process.env.CITADEL_OTP_WAIT_MS || 0);
const DOWNLOAD_DIR = process.env.CITADEL_DOWNLOAD_DIR || '';
const REUSE_TAB = process.env.CITADEL_REUSE_TAB === '1';
const NEW_TAB = process.env.CITADEL_NEW_TAB === '1';
const LOGIN_MODE = process.env.CITADEL_LOGIN_MODE || 'auto';
const AUTH_TRANSPORT = process.env.CITADEL_AUTH_TRANSPORT || 'browser';
const ENVIRONMENT_NAME = process.env.CITADEL_ENVIRONMENT_NAME || 'myservicemac-prod';
const OTP_FILE = process.env.CITADEL_OTP_FILE || path.join(REPORTS, 'citadel_otp.txt');
const OTP_GWS_ENABLED = process.env.CITADEL_OTP_GWS_ENABLED === '1';
const OTP_GWS_SCRIPT = process.env.CITADEL_OTP_GWS_SCRIPT || path.join(ROOT, 'scripts', 'citadel_fetch_email_otp.js');
const OTP_GWS_POLL_INTERVAL_MS = positiveInt(process.env.CITADEL_OTP_GWS_POLL_INTERVAL_MS, 10000);
const HAR_PATH = process.env.CITADEL_HAR_PATH || '/mnt/f/har/citadel_loansphereservicingdigital.bkiconnect.com.har';
const RECAPTCHA_WAIT_MS = Number(process.env.CITADEL_RECAPTCHA_WAIT_MS || 15000);
const TAB_SCAN_LIMIT = positiveInt(process.env.CITADEL_TAB_SCAN_LIMIT, 8);
const TAB_SCAN_FETCH_TIMEOUT_MS = positiveInt(process.env.CITADEL_TAB_SCAN_FETCH_TIMEOUT_MS, 3000);
const TAB_SCAN_TARGET_ID = (process.env.CITADEL_TAB_SCAN_TARGET_ID || '').trim();
const CREDENTIAL_LOGIN_HINT = (process.env.CITADEL_CREDENTIAL_LOGIN_HINT || process.env.CITADEL_BW_LOGIN_HINT || 'coolwoodllc').trim().toLowerCase();
const ALLOW_HAR_AUTH_DIAGNOSTICS = process.env.CITADEL_ALLOW_HAR_AUTH_DIAGNOSTICS === '1';
const USE_HAR_BROWSER_HEADER_DEFAULTS = process.env.CITADEL_USE_HAR_BROWSER_HEADER_DEFAULTS !== '0';
const RECAPTCHA_ACTIONS = (process.env.CITADEL_RECAPTCHA_ACTIONS || 'login,LOGIN')
  .split(',')
  .map(s => s.trim())
  .filter(Boolean);

const configuredReportPath = process.env.CITADEL_REPORT_PATH || '';
const reportPath = configuredReportPath
  ? (path.isAbsolute(configuredReportPath) ? configuredReportPath : path.join(ROOT, configuredReportPath))
  : path.join(REPORTS, 'citadel_statements_download_report.json');
const htmlDumpPath = path.join(REPORTS, 'citadel_statements_failure.html');
const screenshotPath = path.join(REPORTS, 'citadel_statements_failure.png');
const otpRequiredPath = path.join(REPORTS, 'citadel_otp_required.json');
const manualAuthRequiredPath = path.join(REPORTS, 'citadel_manual_auth_required.json');
let lastOtpGwsFetchAt = 0;

function reportStamp(value) {
  const date = value ? new Date(value) : new Date();
  const valid = Number.isFinite(date.getTime()) ? date : new Date();
  return valid.toISOString().replace(/[-:]/g, '').replace(/\.\d{3}Z$/, 'Z');
}

function liveAuthAttemptReportPath(report) {
  const configured = String(process.env.CITADEL_LIVE_AUTH_ATTEMPT_REPORT || '').trim();
  if (configured) return path.isAbsolute(configured) ? configured : path.join(ROOT, configured);
  return path.join(REPORTS, `citadel_live_login_attempt_${reportStamp(report.ended_at || report.started_at)}.json`);
}

function shouldArchiveLiveAuthAttempt(report) {
  if (LOGIN_MODE === 'har') return false;
  if (report && report.idempotent_skip) return false;
  return true;
}

function arrayOrEmpty(value) {
  return Array.isArray(value) ? value : [];
}

function objectOrEmpty(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}

function applyHarWorkflowSummary(report) {
  const diagnostics = objectOrEmpty(report && report.har_workflow_diagnostics);
  if (!Object.keys(diagnostics).length) return report;

  const nextAction = objectOrEmpty(diagnostics.next_action);
  Object.assign(report, {
    har_path: diagnostics.har_path || report.har_path || HAR_PATH || null,
    har_path_exists: diagnostics.har_path_exists === true,
    har_workflow_embedded_response_body_count: diagnostics.embedded_response_body_count ?? 0,
    har_workflow_replayable_json_response_count: diagnostics.replayable_json_response_count ?? 0,
    har_workflow_replayable_document_payload_count: diagnostics.replayable_document_payload_count ?? 0,
    har_workflow_target_month: diagnostics.target_month || report.target_month || null,
    har_workflow_target_month_replayable_document_available: diagnostics.target_month_replayable_document_available === true,
    har_workflow_target_month_replayable_document_payload_count: diagnostics.target_month_replayable_document_payload_count ?? 0,
    har_workflow_replayable_statement_months: arrayOrEmpty(diagnostics.replayable_statement_months),
    har_workflow_statement_document_months: arrayOrEmpty(diagnostics.statement_document_months),
    har_workflow_direct_pdf_response_count: diagnostics.direct_pdf_response_count ?? 0,
    har_workflow_source_direct_pdf_candidate_count: diagnostics.source_direct_pdf_candidate_count ?? 0,
    har_workflow_source_direct_pdf_path_counts: objectOrEmpty(diagnostics.source_direct_pdf_path_counts),
    har_workflow_source_direct_pdf_filenames: arrayOrEmpty(diagnostics.source_direct_pdf_filenames),
    har_workflow_source_direct_pdf_filename_candidates: arrayOrEmpty(diagnostics.source_direct_pdf_filename_candidates),
    har_workflow_target_month_direct_pdf_filenames: arrayOrEmpty(diagnostics.target_month_direct_pdf_filenames),
    har_workflow_target_month_direct_pdf_filename_candidates: arrayOrEmpty(diagnostics.target_month_direct_pdf_filename_candidates),
    har_workflow_target_month_direct_pdf_body_missing_candidate_count: diagnostics.target_month_direct_pdf_body_missing_candidate_count ?? 0,
    har_workflow_source_required_response_candidate_count: diagnostics.source_required_response_candidate_count ?? 0,
    har_workflow_source_required_response_path_counts: objectOrEmpty(diagnostics.source_required_response_path_counts),
    har_workflow_direct_pdf_missing_response_count: diagnostics.direct_pdf_missing_response_count ?? 0,
    har_workflow_direct_pdf_missing_response_paths: arrayOrEmpty(diagnostics.direct_pdf_missing_response_paths),
    har_workflow_capture_quality_status: diagnostics.capture_quality_status || null,
    har_workflow_replay_blocker: diagnostics.replay_blocker || null,
    har_workflow_missing_response_body_count: diagnostics.missing_response_body_count ?? 0,
    har_workflow_missing_response_body_paths: arrayOrEmpty(diagnostics.missing_response_body_paths),
    har_workflow_missing_response_body_path_counts: objectOrEmpty(diagnostics.missing_response_body_path_counts),
    har_workflow_response_body_requirements: arrayOrEmpty(diagnostics.response_body_requirements),
    har_workflow_embedded_access_token_count: diagnostics.embedded_access_token_count ?? 0,
    har_workflow_can_replay_documents: diagnostics.can_replay_documents === true,
    har_workflow_next_action_status: nextAction.status || null,
    har_workflow_next_action_reason: nextAction.reason || null,
    har_workflow_next_action_command: nextAction.next_command || diagnostics.next_command || null,
    har_workflow_next_action_capture_command: nextAction.capture_command || null,
    har_workflow_next_action_capture_required: nextAction.capture_required === true,
    har_workflow_next_action_required_response_paths: arrayOrEmpty(nextAction.required_response_paths),
    har_workflow_next_action_required_response_path_counts: objectOrEmpty(nextAction.required_response_path_counts),
    har_workflow_next_action_response_body_requirements: arrayOrEmpty(nextAction.response_body_requirements),
    har_workflow_next_action_source_direct_pdf_candidate_count: nextAction.source_direct_pdf_candidate_count ?? 0,
    har_workflow_next_action_source_direct_pdf_path_counts: objectOrEmpty(nextAction.source_direct_pdf_path_counts),
    har_workflow_next_action_source_direct_pdf_filenames: arrayOrEmpty(nextAction.source_direct_pdf_filenames),
    har_workflow_next_action_target_month_direct_pdf_filenames: arrayOrEmpty(nextAction.target_month_direct_pdf_filenames),
    har_workflow_next_action_target_month_direct_pdf_body_missing_candidate_count: nextAction.target_month_direct_pdf_body_missing_candidate_count ?? 0,
    har_workflow_next_action_source_required_response_candidate_count: nextAction.source_required_response_candidate_count ?? 0,
    har_workflow_next_action_source_required_response_path_counts: objectOrEmpty(nextAction.source_required_response_path_counts),
    har_workflow_next_action_install_verified_capture_report: nextAction.install_verified_capture_report || null,
    har_workflow_next_action_install_verified_capture_dry_run_command: nextAction.install_verified_capture_dry_run_command || null,
    har_workflow_next_action_install_verified_capture_apply_command: nextAction.install_verified_capture_apply_command || null,
    har_workflow_next_action_install_verified_capture_direct_dry_run_command: nextAction.install_verified_capture_direct_dry_run_command || null,
    har_workflow_next_action_install_verified_capture_direct_apply_command: nextAction.install_verified_capture_direct_apply_command || null,
    install_verified_capture_report: nextAction.install_verified_capture_report || null,
    install_verified_capture_apply_command: nextAction.install_verified_capture_apply_command || null,
    install_verified_capture_direct_apply_command: nextAction.install_verified_capture_direct_apply_command || null,
  });

  if (report.idempotent_skip && report.har_workflow_replay_blocker) {
    report.idempotent_skip_har_replay_attention = true;
  }

  return report;
}

function applyAutomationSafetySummary(report) {
  const diagnostics = objectOrEmpty(report && report.har_workflow_diagnostics);
  const blockers = [];
  const statusOk = report.status === 'ok';
  const targetAvailable = report.target_month_statement_available === true;
  const replayBlocker = report.har_workflow_replay_blocker || diagnostics.replay_blocker || null;
  const canReplayDocuments = report.har_workflow_can_replay_documents === true || diagnostics.can_replay_documents === true;
  const targetReplayable = (
    report.har_workflow_target_month_replayable_document_available === true
    || diagnostics.target_month_replayable_document_available === true
  );
  const harReplayReady = canReplayDocuments && targetReplayable && !replayBlocker;

  if (!statusOk) blockers.push(report.status || 'not_ok');
  if (!targetAvailable) blockers.push('target_month_statement_unavailable');
  if (!harReplayReady) blockers.push(replayBlocker || 'har_replay_not_ready');
  if (LOGIN_MODE !== 'har') blockers.push('default_mode_requires_live_auth_or_local_idempotent_skip');

  report.har_replay_ready_to_run_automatically = harReplayReady;
  report.idempotent_replay_safe = harReplayReady && statusOk && targetAvailable;
  report.copy_plan_safe_to_apply_automatically = false;
  report.safe_to_run_automatically = report.idempotent_replay_safe && LOGIN_MODE === 'har';
  report.automation_blockers = [...new Set(blockers.filter(Boolean))];

  if (report.safe_to_run_automatically) {
    report.automation_readiness_status = 'ready';
  } else if (report.idempotent_skip && targetAvailable && !harReplayReady) {
    report.automation_readiness_status = 'local_statement_available_but_har_replay_blocked';
  } else if (targetAvailable && !harReplayReady) {
    report.automation_readiness_status = 'target_statement_available_but_har_replay_blocked';
  } else if (!targetAvailable) {
    report.automation_readiness_status = 'target_month_statement_unavailable';
  } else {
    report.automation_readiness_status = 'blocked';
  }
  return report;
}

function writeReport(report, options = {}) {
  applyHarWorkflowSummary(report);
  applyAutomationSafetySummary(report);
  const archiveLiveAuth = options.archiveLiveAuth === true && shouldArchiveLiveAuthAttempt(report);
  if (archiveLiveAuth) {
    report.live_auth_attempt_report = liveAuthAttemptReportPath(report);
  }
  fs.writeFileSync(reportPath, JSON.stringify(report, null, 2));
  if (archiveLiveAuth) {
    fs.writeFileSync(report.live_auth_attempt_report, JSON.stringify(report, null, 2));
  }
}

function shellQuote(value) {
  const text = String(value ?? '');
  if (text === '') return "''";
  return `'${text.replace(/'/g, `'\\''`)}'`;
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

function verifiedCaptureInstallFields() {
  const captureHarPath = path.join(REPORTS, 'citadel_replay_capture.har');
  const captureReportPath = path.join(REPORTS, 'citadel_replay_capture_report.json');
  const reportPath = path.join(REPORTS, 'citadel_verified_capture_install_report.json');
  const canonicalHarPath = process.env.CITADEL_CANONICAL_HAR_PATH || HAR_PATH;
  const directCommand = [
    'python3 scripts/install_verified_citadel_capture_har.py',
    `--capture-har ${shellQuote(captureHarPath)}`,
    `--capture-report ${shellQuote(captureReportPath)}`,
    `--canonical-har ${shellQuote(canonicalHarPath)}`,
    `--report ${shellQuote(reportPath)}`,
  ].join(' ');
  return {
    install_verified_capture_report: reportPath,
    install_verified_capture_dry_run_command: buildCaptureWrapperCommand(captureHarPath, captureReportPath, {
      CITADEL_CAPTURE_SKIP_CAPTURE: '1',
      CITADEL_CAPTURE_INSTALL_VERIFIED_HAR: '1',
      CITADEL_CAPTURE_INSTALL_VERIFIED_HAR_ONLY: '1',
      CITADEL_CAPTURE_INSTALL_VERIFIED_HAR_REPORT: reportPath,
      CITADEL_CANONICAL_HAR_PATH: canonicalHarPath,
    }),
    install_verified_capture_apply_command: buildCaptureWrapperCommand(captureHarPath, captureReportPath, {
      CITADEL_CAPTURE_SKIP_CAPTURE: '1',
      CITADEL_CAPTURE_INSTALL_VERIFIED_HAR: '1',
      CITADEL_CAPTURE_INSTALL_VERIFIED_HAR_APPLY: '1',
      CITADEL_CAPTURE_INSTALL_VERIFIED_HAR_ONLY: '1',
      CITADEL_CAPTURE_INSTALL_VERIFIED_HAR_REPORT: reportPath,
      CITADEL_CANONICAL_HAR_PATH: canonicalHarPath,
    }),
    install_verified_capture_direct_dry_run_command: directCommand,
    install_verified_capture_direct_apply_command: `${directCommand} --apply`,
  };
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

function positiveInt(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.trunc(parsed) : fallback;
}

function getCred(field) {
  try {
    return execFileSync('bash', [path.join(ROOT, 'scripts', 'bw_get_citadel.sh'), field], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim();
  } catch (e) {
    return null;
  }
}

function getCredItem() {
  try {
    const text = execFileSync('bash', [path.join(ROOT, 'scripts', 'bw_get_citadel.sh'), 'json'], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim();
    return text ? JSON.parse(text) : null;
  } catch (e) {
    return null;
  }
}

function citadelTargetPriority(target) {
  const url = String((target || {}).url || '');
  const title = String((target || {}).title || '');
  let score = 0;
  if (TAB_SCAN_TARGET_ID && (target || {}).targetId === TAB_SCAN_TARGET_ID) score += 1000;
  if (url.includes('loansphereservicingdigital.bkiconnect.com/mycitadelservicing')) score += 200;
  if (url.includes('mycitadelservicing') || title.toLowerCase().includes('citadel')) score += 100;
  if (!url.includes('#/login')) score += 50;
  if (/#\/(loan|loans|dashboard|home|account|documents|estatements|statements)/i.test(url)) score += 50;
  return score;
}

function prioritizedCitadelTargets(targets) {
  return [...targets].sort((a, b) => {
    const scoreDelta = citadelTargetPriority(b) - citadelTargetPriority(a);
    if (scoreDelta) return scoreDelta;
    return 0;
  });
}

function dropboxRoot() {
  if (process.env.DROPBOX_ROOT) return process.env.DROPBOX_ROOT;
  for (const c of [
    '/mnt/c/Users/digit/Dropbox',
    '/mnt/c/users/digit/Dropbox',
    '/data/Dropbox',
    path.join(process.env.HOME || '', 'Dropbox'),
    '/home/digit/Dropbox',
  ]) {
    if (fs.existsSync(c)) return c;
  }
  return path.join(ROOT, 'Dropbox');
}

function defaultStatementDirCandidates() {
  const root = dropboxRoot();
  return [
    path.join(root, 'Real Estate', 'NY', '90 Madison Ave Albany, NY 12202', 'Public', '04 - Loan Documents'),
    path.join(root, 'Real Estate', 'NY', '90 Madison Ave Public', '04 - Loan Documents'),
    path.join(root, 'Real Estate', 'NY', '90 Madison Ave Public', '07 - P&L & Owner Statements', 'Mortgage Statements'),
  ];
}

function chooseStatementDir(candidates) {
  return candidates.find(c => fs.existsSync(c)) || candidates[0];
}

const STATEMENT_DIR_CANDIDATES = DOWNLOAD_DIR ? [DOWNLOAD_DIR] : defaultStatementDirCandidates();
const STATEMENT_DIR = chooseStatementDir(STATEMENT_DIR_CANDIDATES);

function loadHarHeaderDefaults() {
  const defaults = {};
  if (!USE_HAR_BROWSER_HEADER_DEFAULTS && !ALLOW_HAR_AUTH_DIAGNOSTICS) return defaults;
  if (!HAR_PATH || !fs.existsSync(HAR_PATH)) return defaults;
  try {
    const har = JSON.parse(fs.readFileSync(HAR_PATH, 'utf8'));
    const entries = (((har || {}).log || {}).entries || []);
    const tokenEntry = entries.find(e => String(((e.request || {}).url || '')).includes('/oauth2/v3/token'));
    if (!tokenEntry) return defaults;
    const safeBrowserHeaders = new Set([
      'accept-language',
      'sec-ch-ua',
      'sec-ch-ua-mobile',
      'sec-ch-ua-platform',
      'sec-fetch-dest',
      'sec-fetch-mode',
      'sec-fetch-site',
      'user-agent',
    ]);
    const authDiagnosticHeaders = new Set([
      'x-mobile-source-id',
      'x-mfa-source-id',
      'x-mfa-provider',
    ]);
    for (const header of ((tokenEntry.request || {}).headers || [])) {
      const name = String(header.name || '').toLowerCase();
      if (safeBrowserHeaders.has(name) || (ALLOW_HAR_AUTH_DIAGNOSTICS && authDiagnosticHeaders.has(name))) {
        defaults[name] = header.value;
      }
    }
  } catch {}
  return defaults;
}

const HAR_HEADER_DEFAULTS = loadHarHeaderDefaults();
const ACCEPT_LANGUAGE = process.env.CITADEL_ACCEPT_LANGUAGE || HAR_HEADER_DEFAULTS['accept-language'] || 'en-US,en;q=0.9';
const USER_AGENT = process.env.CITADEL_USER_AGENT || HAR_HEADER_DEFAULTS['user-agent'] || 'Mozilla/5.0 AppleWebKit/537.36 Chrome/149 Safari/537.36';
const SEC_CH_UA = process.env.CITADEL_SEC_CH_UA || HAR_HEADER_DEFAULTS['sec-ch-ua'] || '';
const SEC_CH_UA_MOBILE = process.env.CITADEL_SEC_CH_UA_MOBILE || HAR_HEADER_DEFAULTS['sec-ch-ua-mobile'] || '?0';
const SEC_CH_UA_PLATFORM = process.env.CITADEL_SEC_CH_UA_PLATFORM || HAR_HEADER_DEFAULTS['sec-ch-ua-platform'] || '';
const SEC_FETCH_DEST = process.env.CITADEL_SEC_FETCH_DEST || HAR_HEADER_DEFAULTS['sec-fetch-dest'] || 'empty';
const SEC_FETCH_MODE = process.env.CITADEL_SEC_FETCH_MODE || HAR_HEADER_DEFAULTS['sec-fetch-mode'] || 'cors';
const SEC_FETCH_SITE = process.env.CITADEL_SEC_FETCH_SITE || HAR_HEADER_DEFAULTS['sec-fetch-site'] || 'same-origin';
const MOBILE_SOURCE_ID_FALLBACK = process.env.CITADEL_MOBILE_SOURCE_ID || HAR_HEADER_DEFAULTS['x-mobile-source-id'] || '';
const MFA_SOURCE_ID_FALLBACK = process.env.CITADEL_MFA_SOURCE_ID
  || (process.env.CITADEL_USE_HAR_MFA_SOURCE === '1' ? HAR_HEADER_DEFAULTS['x-mfa-source-id'] : '')
  || '';
const MFA_PROVIDER = process.env.CITADEL_MFA_PROVIDER || HAR_HEADER_DEFAULTS['x-mfa-provider'] || '';
let currentMfaSourceId = MFA_SOURCE_ID_FALLBACK;
let currentMobileSourceId = MOBILE_SOURCE_ID_FALLBACK;
let currentMfaProvider = MFA_PROVIDER;
let authBrowser = null;

function browserStorageAuthHelperSource() {
  return `
    function __citadelStorageAuth() {
      const result = {
        bearerToken: '',
        bearerTokenSource: '',
        mobileSourceId: '',
        localStorageKeyCount: 0,
        sessionStorageKeyCount: 0,
        tokenCandidateCount: 0
      };
      const seen = new Set();
      function consider(value, source) {
        if (value === undefined || value === null) return;
        const text = String(value);
        if (!text || text.length < 16) return;
        const lowerSource = String(source || '').toLowerCase();
        const tokenLikeKey = /token|auth|bearer|access/.test(lowerSource);
        const tokenLikeValue = /^eyJ[a-zA-Z0-9_-]+\\.[a-zA-Z0-9_-]+\\.[a-zA-Z0-9_-]+$/.test(text) || text.length >= 24;
        if (tokenLikeKey && tokenLikeValue) {
          result.tokenCandidateCount += 1;
          if (!result.bearerToken) {
            result.bearerToken = text.replace(/^Bearer\\s+/i, '');
            result.bearerTokenSource = source;
          }
        }
      }
      function walk(value, source, depth) {
        if (value === undefined || value === null || depth > 3) return;
        if (typeof value === 'string') {
          consider(value, source);
          const trimmed = value.trim();
          if ((trimmed.startsWith('{') || trimmed.startsWith('[')) && trimmed.length < 20000) {
            try { walk(JSON.parse(trimmed), source, depth + 1); } catch {}
          }
          return;
        }
        if (typeof value !== 'object' || seen.has(value)) return;
        seen.add(value);
        if (Array.isArray(value)) {
          value.slice(0, 20).forEach((item, i) => walk(item, source + '[' + i + ']', depth + 1));
          return;
        }
        Object.entries(value).forEach(([key, child]) => {
          const childSource = source ? source + '.' + key : key;
          if (/mobile.*source|x-mobile-source-id/.test(String(key).toLowerCase()) && child) {
            result.mobileSourceId = String(child);
          }
          walk(child, childSource, depth + 1);
        });
      }
      for (const [kind, storage] of [['local', window.localStorage], ['session', window.sessionStorage]]) {
        try {
          const countKey = kind === 'local' ? 'localStorageKeyCount' : 'sessionStorageKeyCount';
          result[countKey] = storage ? storage.length : 0;
          for (let i = 0; storage && i < storage.length; i++) {
            const key = storage.key(i);
            const value = storage.getItem(key);
            if (!result.mobileSourceId && key === 'x-mobile-source-id' && value) result.mobileSourceId = value;
            walk(value, kind + '.' + key, 0);
          }
        } catch {}
      }
      return result;
    }
  `;
}

function statementPathForDate(dateStr) {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(dateStr);
  if (!match) return null;
  const year = match[1];
  const dir = path.join(STATEMENT_DIR, year);
  fs.mkdirSync(dir, { recursive: true });
  return path.join(dir, `Mortgage Statement - ${dateStr} - 90 Madison Ave, Albany, NY 12202.pdf`);
}

function listExistingStatementFiles() {
  const files = [];
  const seen = new Set();
  for (const statementDir of STATEMENT_DIR_CANDIDATES) {
    if (!fs.existsSync(statementDir)) continue;
    for (const entry of fs.readdirSync(statementDir)) {
      const full = path.join(statementDir, entry);
      const stat = fs.statSync(full);
      if (stat.isFile() && entry.endsWith('.pdf')) {
        const rel = statementDir === STATEMENT_DIR ? entry : path.relative(STATEMENT_DIR, full);
        const key = path.resolve(full);
        if (!seen.has(key)) {
          files.push(rel);
          seen.add(key);
        }
      }
      if (!stat.isDirectory() || !/^\d{4}$/.test(entry)) continue;
      for (const f of fs.readdirSync(full).filter(name => name.endsWith('.pdf'))) {
        const pdf = path.join(full, f);
        const rel = statementDir === STATEMENT_DIR ? path.join(entry, f) : path.relative(STATEMENT_DIR, pdf);
        const key = path.resolve(pdf);
        if (!seen.has(key)) {
          files.push(rel);
          seen.add(key);
        }
      }
    }
  }
  return files;
}

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

function parseJsonList(text, keys, label, report) {
  const trimmed = String(text || '').trim();
  if (!trimmed) return [];
  try {
    return unwrapArray(JSON.parse(trimmed), keys);
  } catch (e) {
    report.errors.push(`${label} parse error: ${e.message}`);
    return [];
  }
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

function pdfBufferFromBase64(value) {
  const pdfBase64 = normalizeBase64Pdf(value);
  if (!pdfBase64) return null;
  const buffer = Buffer.from(pdfBase64, 'base64');
  return buffer.length > 1000 && buffer.slice(0, 5).toString() === '%PDF-' ? buffer : null;
}

function harPdfBuffer(entry) {
  if (!isSuccessfulResponse(entry && entry.response)) return null;
  const content = entry && entry.response && entry.response.content || {};
  const text = content.text;
  if (!text || !isPdfMime(content.mimeType)) return null;
  const buffer = content.encoding === 'base64' ? Buffer.from(text, 'base64') : Buffer.from(String(text), 'binary');
  return buffer.length > 1000 && buffer.slice(0, 5).toString() === '%PDF-' ? buffer : null;
}

function docField(doc, ...names) {
  for (const name of names) {
    const value = doc && doc[name];
    if (value !== undefined && value !== null && value !== '') return value;
  }
  return undefined;
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

function statementMonthFromName(name) {
  const dateStr = statementDateFromDoc(name, '');
  return dateStr ? dateStr.slice(0, 7) : '';
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
  const dateStr = statementDateFromDoc(docName, docDate);
  return dateStr ? dateStr.slice(0, 7) : '';
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

function sanitizeOtp(value) {
  const otp = String(value || '').replace(/\D/g, '');
  return otp.length >= 4 ? otp : '';
}

async function fetchOtpViaGws(report, afterEpochMs = 0) {
  if (!OTP_GWS_ENABLED) return false;
  const now = Date.now();
  if (lastOtpGwsFetchAt && (now - lastOtpGwsFetchAt) < OTP_GWS_POLL_INTERVAL_MS) return false;
  lastOtpGwsFetchAt = now;
  try {
    const env = { ...process.env };
    if (afterEpochMs) env.CITADEL_OTP_GWS_AFTER_EPOCH_MS = String(afterEpochMs);
    const out = execFileSync(process.execPath, [OTP_GWS_SCRIPT], {
      encoding: 'utf8',
      env,
      stdio: ['ignore', 'pipe', 'pipe'],
      timeout: 90000,
    });
    const parsed = JSON.parse(String(out || '{}').trim());
    if (report) {
      report.otp_gws_enabled = true;
      report.otp_gws_script = OTP_GWS_SCRIPT;
      report.otp_gws_last_status = parsed.status || null;
      report.otp_gws_otp_written = !!parsed.otp_written;
      report.otp_gws_report = parsed.report || null;
    }
    return !!parsed.otp_written;
  } catch (e) {
    if (report) {
      report.otp_gws_enabled = true;
      report.otp_gws_script = OTP_GWS_SCRIPT;
      report.otp_gws_last_status = 'error';
      report.otp_gws_last_error = String(e && e.message || e).slice(0, 300);
    }
    return false;
  }
}

async function readOtp(report, afterEpochMs = 0) {
  const envOtp = sanitizeOtp(process.env.CITADEL_OTP);
  if (envOtp) return envOtp;
  if (OTP_FILE && fs.existsSync(OTP_FILE)) {
    const stat = fs.statSync(OTP_FILE);
    const fileFreshEnough = !afterEpochMs || stat.mtimeMs >= afterEpochMs;
    const fileOtp = fileFreshEnough ? sanitizeOtp(fs.readFileSync(OTP_FILE, 'utf8')) : '';
    if (fileOtp) return fileOtp;
  }
  await fetchOtpViaGws(report, afterEpochMs);
  if (OTP_FILE && fs.existsSync(OTP_FILE)) {
    const stat = fs.statSync(OTP_FILE);
    if (!afterEpochMs || stat.mtimeMs >= afterEpochMs) {
      return sanitizeOtp(fs.readFileSync(OTP_FILE, 'utf8'));
    }
  }
  return '';
}

async function waitForOtp(report) {
  const start = Date.now();
  const freshAfterEpochMs = Math.max(0, start - 60000);
  while ((Date.now() - start) <= OTP_WAIT_MS) {
    const otp = await readOtp(report, freshAfterEpochMs);
    if (otp) return otp;
    if (OTP_WAIT_MS <= 0) break;
    await new Promise(r => setTimeout(r, 2000));
  }
  report.otp_required = true;
  report.otp_wait_ms = OTP_WAIT_MS;
  report.otp_file_configured = !!OTP_FILE;
  report.otp_file = OTP_FILE || null;
  report.otp_gws_enabled = OTP_GWS_ENABLED;
  report.otp_gws_script = OTP_GWS_SCRIPT;
  report.otp_required_file = otpRequiredPath;
  report.otp_next_command = `CITADEL_OTP_FILE=${JSON.stringify(OTP_FILE)} CITADEL_OTP_WAIT_MS=120000 scripts/baselane_monthly_mortgage_workflow_idempotent.sh`;
  fs.writeFileSync(otpRequiredPath, JSON.stringify({
    job: 'citadel-otp-required',
    created_at: new Date().toISOString(),
    otp_file_configured: !!OTP_FILE,
    otp_file: OTP_FILE || null,
    otp_wait_ms: OTP_WAIT_MS,
    next_command: report.otp_next_command,
  }, null, 2));
  return '';
}

function writeManualAuthRequired(report, reason) {
  report.manual_auth_required = true;
  report.manual_auth_reason = reason || null;
  report.manual_auth_blocker = report.auth_failure_reason || reason || null;
  report.manual_auth_visible_reason = report.auth_failure_visible_reason || null;
  report.manual_auth_portal_url = PORTAL_URL;
  report.manual_auth_file = manualAuthRequiredPath;
  const selectedTargetId = report.citadel_tab_scan && report.citadel_tab_scan.selected_target_id || '';
  const nextRunEnv = {
    CITADEL_LOGIN_MODE: 'manual',
    CITADEL_REUSE_TAB: '1',
    CITADEL_AUTH_WAIT_MS: '300000',
    CITADEL_PRUNE_LOGIN_TABS: '1',
  };
  if (selectedTargetId) nextRunEnv.CITADEL_TAB_SCAN_TARGET_ID = selectedTargetId;
  fs.writeFileSync(manualAuthRequiredPath, JSON.stringify({
    job: 'citadel-manual-auth-required',
    created_at: new Date().toISOString(),
    reason: reason || null,
    auth_failure_reason: report.auth_failure_reason || null,
    auth_failure_visible_reason: report.auth_failure_visible_reason || null,
    auth_visible_error: report.auth_visible_error || null,
    portal_url: PORTAL_URL,
    expected_existing_tab: true,
    selected_target_id: selectedTargetId || null,
    next_run_env: nextRunEnv,
    report_path: reportPath,
  }, null, 2));
}

function formBody(fields) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(fields)) {
    if (value !== undefined && value !== null && value !== '') params.set(key, value);
  }
  return params;
}

const OAUTH_REQUEST_SHAPE_HEADERS = [
  'accept',
  'accept-language',
  'content-type',
  'origin',
  'referer',
  'sec-ch-ua',
  'sec-ch-ua-mobile',
  'sec-ch-ua-platform',
  'sec-fetch-dest',
  'sec-fetch-mode',
  'sec-fetch-site',
  'user-agent',
  'x-correlation-id',
  'x-mobile-source-id',
  'x-mfa-provider',
  'x-mfa-source-id',
  'x-mfa-process-id',
];
const OAUTH_VOLATILE_HEADER_LENGTHS = new Set([
  'accept-language',
  'sec-ch-ua-platform',
  'user-agent',
  'x-correlation-id',
  'x-mfa-source-id',
]);

function lowerHeaderMap(headers) {
  const mapped = {};
  if (Array.isArray(headers)) {
    for (const header of headers) {
      const name = String(header && header.name || '').toLowerCase();
      if (name) mapped[name] = String(header && header.value || '');
    }
    return mapped;
  }
  if (headers && typeof headers === 'object') {
    for (const [name, value] of Object.entries(headers)) {
      const key = String(name || '').toLowerCase();
      if (key) mapped[key] = String(value || '');
    }
  }
  return mapped;
}

function oauthRequestShape(headers, bodyText) {
  const headerMap = lowerHeaderMap(headers);
  const params = new URLSearchParams(String(bodyText || ''));
  const bodyKeys = [...new Set([...params.keys()])].sort();
  const bodyLengths = {};
  for (const key of bodyKeys) bodyLengths[key] = String(params.get(key) || '').length;
  const headerPresent = {};
  const headerLengths = {};
  for (const key of OAUTH_REQUEST_SHAPE_HEADERS) {
    headerPresent[key] = Object.prototype.hasOwnProperty.call(headerMap, key);
    headerLengths[key] = headerPresent[key] ? headerMap[key].length : 0;
  }
  return {
    body_keys: bodyKeys,
    body_lengths: bodyLengths,
    header_present: headerPresent,
    header_lengths: headerLengths,
  };
}

function oauthShapeComparable(shape) {
  if (!shape || typeof shape !== 'object') return null;
  return {
    body_keys: shape.body_keys || [],
    body_lengths: shape.body_lengths || {},
    header_present: shape.header_present || {},
    header_lengths: shape.header_lengths || {},
  };
}

function oauthShapeComparableStructure(shape) {
  const comparable = oauthShapeComparable(shape);
  if (!comparable) return null;
  const headerLengths = { ...comparable.header_lengths };
  for (const key of OAUTH_VOLATILE_HEADER_LENGTHS) delete headerLengths[key];
  return {
    ...comparable,
    header_lengths: headerLengths,
  };
}

function oauthShapeMatches(left, right, mode = 'exact') {
  const a = oauthShapeComparable(left);
  const b = oauthShapeComparable(right);
  if (!a || !b) return false;
  if (mode === 'structure') {
    return JSON.stringify(oauthShapeComparableStructure(a)) === JSON.stringify(oauthShapeComparableStructure(b));
  }
  return JSON.stringify(a) === JSON.stringify(b);
}

function oauthShapeHarComparison(shape, credentialDiagnostics) {
  const attempts = credentialDiagnostics && Array.isArray(credentialDiagnostics.har_token_attempts)
    ? credentialDiagnostics.har_token_attempts
    : [];
  const structureMatches = attempts.filter(attempt => attempt && oauthShapeMatches(shape, attempt.request_shape, 'structure'));
  return {
    success: attempts.some(attempt => attempt && attempt.status === 200 && oauthShapeMatches(shape, attempt.request_shape)),
    failure: attempts.some(attempt => attempt && attempt.status !== 200 && oauthShapeMatches(shape, attempt.request_shape)),
    matched_statuses: [...new Set(attempts
      .filter(attempt => attempt && oauthShapeMatches(shape, attempt.request_shape))
      .map(attempt => attempt.status)
    )].sort(),
    structure_success: structureMatches.some(attempt => attempt.status === 200),
    structure_failure: structureMatches.some(attempt => attempt.status !== 200),
    structure_matched_statuses: [...new Set(structureMatches.map(attempt => attempt.status))].sort(),
    structure_initial_mfa_challenge: structureMatches.some(attempt => {
      const requestShape = attempt.request_shape || {};
      const headerPresent = requestShape.header_present || {};
      return attempt.status === 401 && headerPresent['x-mfa-source-id'] === true && headerPresent['x-mfa-process-id'] !== true;
    }),
  };
}

async function readResponse(resp) {
  const text = await resp.text();
  let json = null;
  if (text) {
    try {
      json = JSON.parse(text);
    } catch {}
  }
  return {
    status: resp.status,
    ok: resp.ok,
    text,
    json,
    mfaProcessIdHeader: resp.headers.get('x-mfa-process-id') || '',
  };
}

async function createCdpPage(url, report, label) {
  const version = await (await fetch(VERSION_URL)).json();
  const ws = new WebSocket(version.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  const sessions = new Map();
  const oauthEvents = [];
  let sessionId = null;

  function send(method, params = {}, sessId) {
    const msg = { id: ++id, method, params };
    if (sessId) msg.sessionId = sessId;
    ws.send(JSON.stringify(msg));
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(msg.id);
        reject(new Error(`CDP command timed out: ${method}`));
      }, TIMEOUT_MS);
      pending.set(msg.id, {
        resolve: value => {
          clearTimeout(timer);
          resolve(value);
        },
        reject: error => {
          clearTimeout(timer);
          reject(error);
        },
      });
    });
  }

  ws.onmessage = ev => {
    const msg = JSON.parse(ev.data);
    if (msg.id) {
      const p = pending.get(msg.id);
      if (p) {
        pending.delete(msg.id);
        if (msg.error) p.reject(new Error(JSON.stringify(msg.error)));
        else p.resolve(msg.result);
      }
      return;
    }
    if (msg.method === 'Target.attachedToTarget') {
      sessions.set(msg.params.targetInfo.targetId, msg.params.sessionId);
    }
    if (msg.sessionId === sessionId && msg.method === 'Network.responseReceived') {
      const params = msg.params || {};
      const response = params.response || {};
      const url = response.url || '';
      if (url.includes('/oauth2/v3/token')) {
        const headers = response.headers || {};
        const headerValue = name => {
          const wanted = String(name).toLowerCase();
          for (const [key, value] of Object.entries(headers)) {
            if (String(key).toLowerCase() === wanted) return String(value || '');
          }
          return '';
        };
        const mfaProcessIdHeader = headerValue('x-mfa-process-id');
        oauthEvents.push({
          requestId: params.requestId,
          status: response.status,
          urlPath: (() => {
            try { return new URL(url).pathname; } catch { return url; }
          })(),
          mfaProcessIdHeader,
          mfaProcessIdHeaderLen: mfaProcessIdHeader.length,
        });
      }
    }
  };

  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('CDP websocket open timed out')), TIMEOUT_MS);
    ws.onopen = () => {
      clearTimeout(timer);
      resolve();
    };
    ws.onerror = error => {
      clearTimeout(timer);
      reject(error);
    };
  });
  await send('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: false, flatten: true });
  const created = await send('Target.createTarget', { url });
  const targetId = created.targetId;
  await send('Target.activateTarget', { targetId }).catch(() => null);
  for (let i = 0; i < 120; i++) {
    sessionId = sessions.get(targetId);
    if (sessionId) break;
    await new Promise(r => setTimeout(r, 100));
  }
  if (!sessionId) {
    const attached = await send('Target.attachToTarget', { targetId, flatten: true });
    sessionId = attached.sessionId;
  }
  await send('Page.enable', {}, sessionId);
  await send('Runtime.enable', {}, sessionId);
  await send('Network.enable', {}, sessionId);
  await send('Page.bringToFront', {}, sessionId).catch(() => null);
  if (report && label) {
    report.direct_auth = report.direct_auth || {};
    report.direct_auth[`${label}_cdp_page_created`] = true;
  }

  async function evalExpr(expression, awaitPromise = true) {
    const res = await send('Runtime.evaluate', { expression, awaitPromise, returnByValue: true }, sessionId);
    if (res.result && res.result.subtype === 'error') throw new Error(res.result.description || 'eval error');
    return res.result ? res.result.value : undefined;
  }

  async function close() {
    if (ws.readyState === WebSocket.OPEN) {
      try {
        const closeId = ++id;
        ws.send(JSON.stringify({ id: closeId, method: 'Target.closeTarget', params: { targetId } }));
        await new Promise(resolve => {
          const timer = setTimeout(resolve, 2000);
          pending.set(closeId, {
            resolve: value => {
              clearTimeout(timer);
              resolve(value);
            },
            reject: () => {
              clearTimeout(timer);
              resolve();
            },
          });
        });
      } catch {}
    }
    try { ws.close(); } catch {}
  }

  return { evalExpr, close, send, sessionId, targetId, oauthEvents };
}

function findFirstUuid(value) {
  const seen = new Set();
  const uuidRe = /[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/i;
  function walk(v) {
    if (v === null || v === undefined) return '';
    if (typeof v === 'string') {
      const m = uuidRe.exec(v);
      return m ? m[0] : '';
    }
    if (typeof v !== 'object' || seen.has(v)) return '';
    seen.add(v);
    for (const child of Object.values(v)) {
      const found = walk(child);
      if (found) return found;
    }
    return '';
  }
  return walk(value);
}

function directAuthErrorSummary(payload) {
  if (!payload || typeof payload !== 'object') return {};
  const summary = {};
  for (const key of ['error', 'errorCode', 'ErrorCode', 'code', 'Code']) {
    if (payload[key] !== undefined) summary.error_code = String(payload[key]);
  }
  for (const key of ['error_description', 'errorDescription', 'ErrorDescription', 'message', 'Message']) {
    if (payload[key] !== undefined) summary.error_message = String(payload[key]).slice(0, 200);
  }
  const errorMessages = payload.ERROR_MESSAGES || payload.error_messages || payload.ErrorMessages;
  if (Array.isArray(errorMessages)) {
    summary.error_messages_count = errorMessages.length;
    summary.error_message_keys = [...new Set(errorMessages.flatMap(item => item && typeof item === 'object' ? Object.keys(item) : []))].sort();
    summary.error_message_codes = [...new Set(errorMessages.flatMap(item => {
      if (!item || typeof item !== 'object') return [];
      return ['code', 'Code', 'errorCode', 'ErrorCode', 'error_code', 'ErrorMessageCode'].map(key => item[key]).filter(Boolean).map(String);
    }))].sort();
    summary.error_message_categories = [...new Set(errorMessages.flatMap(item => {
      if (!item || typeof item !== 'object') return [];
      return ['category', 'Category', 'ErrorMessageCategoryCode'].map(key => item[key]).filter(Boolean).map(String);
    }))].sort();
  }
  summary.response_keys = Object.keys(payload).sort();
  return summary;
}

function extractMfaDetail(payload) {
  const detail = payload && typeof payload === 'object' ? (payload.MFADetail || payload.mfaDetail || payload.MfaDetail) : null;
  if (!detail || typeof detail !== 'object') return null;
  return {
    request_uuid: String(detail.RequestUUID || detail.requestUUID || detail.requestUuid || ''),
    masked_email_available: !!(detail.MaskedContactPointEmailValue || detail.maskedContactPointEmailValue),
    masked_phone_available: !!(detail.MaskedPrimaryTelephoneNumber || detail.maskedPrimaryTelephoneNumber),
    send_via_types: Array.isArray(detail.SendViaTypes || detail.sendViaTypes) ? (detail.SendViaTypes || detail.sendViaTypes).map(String) : [],
  };
}

function chooseOtpSendViaType(mfaDetail) {
  const configured = process.env.CITADEL_OTP_SEND_VIA_TYPE;
  if (configured) return configured;
  const types = (mfaDetail && Array.isArray(mfaDetail.send_via_types) ? mfaDetail.send_via_types : []).filter(Boolean);
  const emailType = types.find(type => /^email$/i.test(type));
  return emailType ? String(emailType).toLowerCase() : (types[0] || 'email');
}

function otpRequestShape(headers, body) {
  const normalizedHeaders = Object.fromEntries(Object.entries(headers || {}).map(([key, value]) => [String(key).toLowerCase(), String(value || '')]));
  return {
    body_keys: Object.keys(body || {}).sort(),
    request_uuid_len: String((body || {}).RequestUUID || '').length,
    send_via_type: String((body || {}).SendViaType || ''),
    send_via_type_is_email_lowercase: String((body || {}).SendViaType || '') === 'email',
    has_authorization_header: !!normalizedHeaders.authorization,
    has_mobile_source_header: !!normalizedHeaders['x-mobile-source-id'],
    has_correlation_id_header: !!normalizedHeaders['x-correlation-id'],
    content_type: normalizedHeaders['content-type'] || '',
    accept: normalizedHeaders.accept || '',
  };
}

function otpRequestShapeMatchesHar(shape, diagnostics) {
  const attempts = (diagnostics && Array.isArray(diagnostics.har_otp_send_attempts)) ? diagnostics.har_otp_send_attempts : [];
  return attempts.some(attempt => (
    attempt.status === 200
    && Array.isArray(attempt.body_keys)
    && JSON.stringify(attempt.body_keys) === JSON.stringify(shape.body_keys)
    && attempt.request_uuid_len === shape.request_uuid_len
    && attempt.send_via_type === shape.send_via_type
    && attempt.has_authorization_header === shape.has_authorization_header
    && attempt.has_mobile_source_header === shape.has_mobile_source_header
    && attempt.has_correlation_id_header === shape.has_correlation_id_header
    && attempt.content_type === shape.content_type
    && attempt.accept === shape.accept
  ));
}

function harResponseText(entry) {
  const content = (((entry || {}).response || {}).content || {});
  const text = content.text || '';
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
  const content = (((entry || {}).response || {}).content || {});
  const text = harResponseText(entry);
  if (!text || textLooksHtml(text)) return null;
  if (content.mimeType && !isJsonMime(content.mimeType)) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function harWorkflowNextAction(diagnostics) {
  const installFields = verifiedCaptureInstallFields();
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
      required_response_paths: diagnostics.missing_response_body_paths,
      required_response_path_counts: diagnostics.missing_response_body_path_counts || {},
      response_body_requirements: diagnostics.response_body_requirements || [],
      source_direct_pdf_candidate_count: diagnostics.source_direct_pdf_candidate_count || 0,
      source_direct_pdf_path_counts: diagnostics.source_direct_pdf_path_counts || {},
      source_direct_pdf_filenames: diagnostics.source_direct_pdf_filenames || [],
      source_direct_pdf_filename_candidates: diagnostics.source_direct_pdf_filename_candidates || [],
      target_month_direct_pdf_filenames: diagnostics.target_month_direct_pdf_filenames || [],
      target_month_direct_pdf_filename_candidates: diagnostics.target_month_direct_pdf_filename_candidates || [],
      target_month_direct_pdf_body_missing_candidate_count: diagnostics.target_month_direct_pdf_body_missing_candidate_count || 0,
      source_required_response_candidate_count: diagnostics.source_required_response_candidate_count || 0,
      source_required_response_path_counts: diagnostics.source_required_response_path_counts || {},
      ...installFields,
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
      replayable_statement_months: diagnostics.replayable_statement_months || [],
      statement_document_months: diagnostics.statement_document_months || [],
      required_response_paths: diagnostics.missing_response_body_paths || [],
      required_response_path_counts: diagnostics.missing_response_body_path_counts || {},
      response_body_requirements: diagnostics.response_body_requirements || [],
      source_direct_pdf_candidate_count: diagnostics.source_direct_pdf_candidate_count || 0,
      source_direct_pdf_path_counts: diagnostics.source_direct_pdf_path_counts || {},
      source_direct_pdf_filenames: diagnostics.source_direct_pdf_filenames || [],
      source_direct_pdf_filename_candidates: diagnostics.source_direct_pdf_filename_candidates || [],
      target_month_direct_pdf_filenames: diagnostics.target_month_direct_pdf_filenames || [],
      target_month_direct_pdf_filename_candidates: diagnostics.target_month_direct_pdf_filename_candidates || [],
      target_month_direct_pdf_body_missing_candidate_count: diagnostics.target_month_direct_pdf_body_missing_candidate_count || 0,
      source_required_response_candidate_count: diagnostics.source_required_response_candidate_count || 0,
      source_required_response_path_counts: diagnostics.source_required_response_path_counts || {},
      ...installFields,
    };
  }
  if (diagnostics.capture_quality_status === 'missing_har') {
    return {
      status: 'provide_workflow_har',
      reason: diagnostics.replay_blocker,
      next_command: 'scripts/citadel_manual_capture_har.sh',
      capture_required: true,
      required_response_paths: [],
      required_response_path_counts: {},
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
    required_response_paths: diagnostics.missing_response_body_paths || [],
    required_response_path_counts: diagnostics.missing_response_body_path_counts || {},
    source_direct_pdf_candidate_count: diagnostics.source_direct_pdf_candidate_count || 0,
    source_direct_pdf_path_counts: diagnostics.source_direct_pdf_path_counts || {},
    source_direct_pdf_filenames: diagnostics.source_direct_pdf_filenames || [],
    source_direct_pdf_filename_candidates: diagnostics.source_direct_pdf_filename_candidates || [],
    target_month_direct_pdf_filenames: diagnostics.target_month_direct_pdf_filenames || [],
    target_month_direct_pdf_filename_candidates: diagnostics.target_month_direct_pdf_filename_candidates || [],
    target_month_direct_pdf_body_missing_candidate_count: diagnostics.target_month_direct_pdf_body_missing_candidate_count || 0,
    source_required_response_candidate_count: diagnostics.source_required_response_candidate_count || 0,
    source_required_response_path_counts: diagnostics.source_required_response_path_counts || {},
    ...installFields,
  };
}

function harWorkflowDiagnostics() {
  const diagnostics = {
    har_path: HAR_PATH,
    har_path_exists: !!(HAR_PATH && fs.existsSync(HAR_PATH)),
    target_month: currentTargetMonth(),
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
    embedded_access_token_count: 0,
    statement_list_body_available: false,
    document_list_body_available: false,
    document_detail_body_count: 0,
    document_detail_embedded_pdf_count: 0,
    can_replay_documents: false,
  };
  if (!diagnostics.har_path_exists) {
    diagnostics.capture_quality_status = 'missing_har';
    diagnostics.replay_blocker = 'har_path_unavailable';
    diagnostics.next_action = harWorkflowNextAction(diagnostics);
    diagnostics.next_command = diagnostics.next_action.next_command;
    return diagnostics;
  }
  try {
    const har = JSON.parse(fs.readFileSync(HAR_PATH, 'utf8'));
    const entries = (((har || {}).log || {}).entries || []);
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
            statement_month: statementMonth || null,
            content_size: Number(content.size || 0),
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
    diagnostics.response_body_requirements = buildHarResponseBodyRequirements(diagnostics);
    diagnostics.source_required_response_candidate_count = diagnostics.missing_response_body_count;
    diagnostics.source_required_response_path_counts = { ...diagnostics.missing_response_body_path_counts };
    diagnostics.can_replay_documents = diagnostics.target_month_replayable_document_available;
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
  } catch (e) {
    diagnostics.har_parse_error = e.message;
    diagnostics.capture_quality_status = 'parse_error';
    diagnostics.replay_blocker = 'har_parse_error';
  }
  diagnostics.next_action = harWorkflowNextAction(diagnostics);
  diagnostics.next_command = diagnostics.next_action.next_command;
  return diagnostics;
}

function harResponseBodyRequirementRole(safePath) {
  const path = String(safePath || '');
  if (path.includes('/LISViewDoc.aspx')) return 'direct_pdf';
  if (path.includes(`/sd/loan-maint/v2/my/loans/${LOAN_ORG}/${LOAN_ID}/eStatements`)) return 'statement_index';
  if (path.endsWith(`/lsd/api/v1/my/loans/${LOAN_ORG}/${LOAN_ID}/docs`)) return 'document_list';
  if (path.includes(`/lsd/api/v1/my/loans/${LOAN_ORG}/${LOAN_ID}/docs/`)) return 'document_detail';
  return 'workflow_response';
}

function buildHarResponseBodyRequirements(diagnostics) {
  const missingCounts = diagnostics.missing_response_body_path_counts || {};
  const directPdfCounts = diagnostics.direct_pdf_missing_response_path_counts || {};
  return Object.keys(missingCounts).sort().map(safePath => {
    const role = harResponseBodyRequirementRole(safePath);
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

function credentialItemSummary(item) {
  if (!item || typeof item !== 'object') return {};
  const login = item.login || {};
  const portalHost = new URL(PORTAL_URL).host.toLowerCase();
  const username = String(login.username || '');
  const uriHosts = [];
  for (const uriEntry of login.uris || []) {
    const uri = String((uriEntry || {}).uri || '');
    if (!uri) continue;
    try {
      const host = new URL(uri).host;
      if (host && !uriHosts.includes(host)) uriHosts.push(host);
    } catch {}
  }
  return {
    credential_item_name: item.name || null,
    credential_item_uri_hosts: uriHosts,
    credential_item_portal_host_match: uriHosts.map(host => String(host).toLowerCase()).includes(portalHost),
    credential_item_field_names: (item.fields || [])
      .map(field => field && field.name)
      .filter(Boolean),
    credential_item_notes_len: String(item.notes || '').length,
    credential_login_hint_configured: !!CREDENTIAL_LOGIN_HINT,
    credential_username_matches_login_hint: !!(CREDENTIAL_LOGIN_HINT && username.toLowerCase() === CREDENTIAL_LOGIN_HINT),
    credential_item_name_matches_login_hint: !!(CREDENTIAL_LOGIN_HINT && String(item.name || '').toLowerCase() === CREDENTIAL_LOGIN_HINT),
    credential_field_matches_login_hint: !!(CREDENTIAL_LOGIN_HINT && (item.fields || []).some(field => {
      const name = String((field || {}).name || '').toLowerCase();
      const value = String((field || {}).value || '').toLowerCase();
      return name === CREDENTIAL_LOGIN_HINT || value === CREDENTIAL_LOGIN_HINT;
    })),
    credential_item_revisionDate: item.revisionDate || null,
    credential_item_resolution: item._citadel_resolution || null,
    username_len: username ? username.length : 0,
    password_len: login.password ? String(login.password).length : 0,
    username_has_at: !!(username && username.includes('@')),
  };
}

function credentialDiagnostics(creds, itemSummary = null) {
  const summary = itemSummary || {};
  const diagnostics = {
    credential_item_name: summary.credential_item_name || null,
    credential_item_uri_hosts: summary.credential_item_uri_hosts || [],
    credential_item_portal_host_match: summary.credential_item_portal_host_match || false,
    credential_item_field_names: summary.credential_item_field_names || [],
    credential_item_notes_len: summary.credential_item_notes_len || 0,
    credential_login_hint_configured: summary.credential_login_hint_configured || false,
    credential_username_matches_login_hint: summary.credential_username_matches_login_hint || false,
    credential_item_name_matches_login_hint: summary.credential_item_name_matches_login_hint || false,
    credential_field_matches_login_hint: summary.credential_field_matches_login_hint || false,
    credential_item_revisionDate: summary.credential_item_revisionDate || null,
    credential_item_resolution: summary.credential_item_resolution || null,
    username_len: summary.username_len || (creds && creds.email ? String(creds.email).length : 0),
    password_len: summary.password_len || (creds && creds.password ? String(creds.password).length : 0),
    username_has_at: summary.username_has_at || !!(creds && creds.email && String(creds.email).includes('@')),
    har_path: HAR_PATH,
    har_path_exists: !!(HAR_PATH && fs.existsSync(HAR_PATH)),
    har_browser_header_defaults_enabled: USE_HAR_BROWSER_HEADER_DEFAULTS,
    har_browser_header_defaults_loaded: Object.keys(HAR_HEADER_DEFAULTS).length > 0,
    har_token_attempts: [],
    har_token_credential_match_count: 0,
    har_successful_password_token_match: false,
    har_mfa_process_id_header_count: 0,
    har_otp_send_attempts: [],
    har_successful_otp_send_count: 0,
    har_auth_diagnostics_enabled: ALLOW_HAR_AUTH_DIAGNOSTICS,
    har_auth_diagnostics_skipped_reason: ALLOW_HAR_AUTH_DIAGNOSTICS ? null : 'disabled_har_workflow_evidence_only',
  };
  if (!ALLOW_HAR_AUTH_DIAGNOSTICS) return diagnostics;
  if (!diagnostics.har_path_exists || !creds) return diagnostics;
  try {
    const har = JSON.parse(fs.readFileSync(HAR_PATH, 'utf8'));
    const entries = (((har || {}).log || {}).entries || []);
    for (let index = 0; index < entries.length; index++) {
      const entry = entries[index] || {};
      const request = entry.request || {};
      const response = entry.response || {};
      const url = String(request.url || '');
      const method = String(request.method || '');
      const postText = String(((request.postData || {}).text || ''));
      const headers = request.headers || [];
      const headerObject = Object.fromEntries(headers.map(h => [String(h.name || '').toLowerCase(), String(h.value || '')]));
      if (url.includes('/sd/preauth/ssp/v1/process/otps') && method === 'POST' && !url.includes('/verify')) {
        let body = {};
        try {
          body = postText ? JSON.parse(postText) : {};
        } catch {}
        const shape = otpRequestShape(headerObject, body);
        diagnostics.har_otp_send_attempts.push({
          index,
          status: response.status,
          ...shape,
        });
        if (response.status === 200) diagnostics.har_successful_otp_send_count += 1;
      }
      if (!url.includes('/oauth2/v3/token')) continue;
      const params = new URLSearchParams(postText);
      if (!params.has('username') && !params.has('password')) continue;
      const username = params.get('username') || '';
      const password = params.get('password') || '';
      const hasHeader = name => headers.some(h => String(h.name || '').toLowerCase() === name);
      const headerValue = name => {
        const found = headers.find(h => String(h.name || '').toLowerCase() === name);
        return found ? String(found.value || '') : '';
      };
      const attempt = {
        index,
        status: response.status,
        username_len: username.length,
        password_len: password.length,
        username_matches_current: username === creds.email,
        password_matches_current: password === creds.password,
        has_mobile_source_header: hasHeader('x-mobile-source-id'),
        has_mfa_source_header: hasHeader('x-mfa-source-id'),
        has_mfa_process_id_header: hasHeader('x-mfa-process-id'),
        mfa_process_id_header_len: headerValue('x-mfa-process-id').length,
        request_shape: oauthRequestShape(headers, postText),
      };
      diagnostics.har_token_attempts.push(attempt);
      if (attempt.has_mfa_process_id_header) diagnostics.har_mfa_process_id_header_count += 1;
      if (attempt.username_matches_current && attempt.password_matches_current) {
        diagnostics.har_token_credential_match_count += 1;
        if (attempt.status === 200) diagnostics.har_successful_password_token_match = true;
      }
    }
  } catch (e) {
    diagnostics.har_parse_error = e.message;
  }
  return diagnostics;
}

function credentialStateDriftAssessment(report, rejectedBeforeMfa) {
  const diagnostics = (report && report.credential_diagnostics) || {};
  if (!rejectedBeforeMfa) {
    return {
      suspected: false,
      checked: false,
      basis: 'not_rejected_before_mfa',
    };
  }
  if (!diagnostics.har_auth_diagnostics_enabled) {
    return {
      suspected: false,
      checked: false,
      basis: diagnostics.har_auth_diagnostics_skipped_reason || 'har_auth_diagnostics_disabled',
    };
  }
  if (diagnostics.har_successful_password_token_match) {
    return {
      suspected: false,
      checked: true,
      basis: 'portal_auth_rejected_current_har_matched_credentials',
    };
  }
  if (Number(diagnostics.har_token_credential_match_count || 0) > 0) {
    return {
      suspected: false,
      checked: true,
      basis: 'current_credentials_match_har_password_request_without_successful_match',
    };
  }
  return {
    suspected: false,
    checked: true,
    basis: 'current_credentials_do_not_match_successful_har_password_request',
  };
}

function applyCredentialStateDriftAssessment(target, report, rejectedBeforeMfa) {
  const assessment = credentialStateDriftAssessment(report, rejectedBeforeMfa);
  target.credential_state_drift_suspected = assessment.suspected;
  target.credential_state_drift_checked = assessment.checked;
  target.credential_state_drift_basis = assessment.basis;
  return assessment;
}

function browserHeaders(contentType) {
  const headers = {
    accept: 'application/json',
    'accept-language': ACCEPT_LANGUAGE,
    origin: new URL(PORTAL_URL).origin,
    referer: `${new URL(PORTAL_URL).origin}/mycitadelservicing/`,
    'sec-fetch-dest': SEC_FETCH_DEST,
    'sec-fetch-mode': SEC_FETCH_MODE,
    'sec-fetch-site': SEC_FETCH_SITE,
    'user-agent': USER_AGENT,
    'x-correlation-id': randomUUID(),
  };
  if (contentType) headers['content-type'] = contentType;
  if (SEC_CH_UA) headers['sec-ch-ua'] = SEC_CH_UA;
  if (SEC_CH_UA_MOBILE) headers['sec-ch-ua-mobile'] = SEC_CH_UA_MOBILE;
  if (SEC_CH_UA_PLATFORM) headers['sec-ch-ua-platform'] = SEC_CH_UA_PLATFORM;
  if (currentMobileSourceId) headers['x-mobile-source-id'] = currentMobileSourceId;
  return headers;
}

function oauthHeaders(mfaProcessId = '') {
  const headers = browserHeaders('application/x-www-form-urlencoded');
  if (currentMfaSourceId) headers['x-mfa-source-id'] = currentMfaSourceId;
  if (currentMfaProvider) headers['x-mfa-provider'] = currentMfaProvider;
  if (mfaProcessId) headers['x-mfa-process-id'] = mfaProcessId;
  return headers;
}

function oauthHeaderObject(mfaProcessId = '') {
  return oauthHeaders(mfaProcessId);
}

async function getAuthBrowser(report) {
  if (!authBrowser) {
    authBrowser = await createCdpPage(PORTAL_URL, report, 'auth');
  }
  return authBrowser;
}

async function browserOauthToken(creds, report, mfaProcessId = '') {
  const browser = await getAuthBrowser(report);
  const headerObject = oauthHeaderObject(mfaProcessId);
  const eventStart = browser.oauthEvents.length;
  const body = String(formBody({
    grant_type: 'password',
    username: creds.email,
    password: creds.password,
    environment_name: ENVIRONMENT_NAME,
  }));
  const requestShape = oauthRequestShape(headerObject, body);
  const result = await browser.evalExpr(`
    (async () => {
      const headers = ${JSON.stringify(headerObject)};
      const resp = await fetch(${JSON.stringify(`${new URL(PORTAL_URL).origin}/mycitadelservicing/webapi/oauth2/v3/token`)}, {
        method: 'POST',
        credentials: 'include',
        headers,
        body: ${JSON.stringify(body)}
      });
      const text = await resp.text();
      let json = null;
      try { json = text ? JSON.parse(text) : null; } catch {}
      return {
        status: resp.status,
        ok: resp.ok,
        text,
        json,
        mfaProcessIdHeader: resp.headers.get('x-mfa-process-id') || ''
      };
    })()
  `);
  result.requestShape = requestShape;
  const cdpEvent = browser.oauthEvents.slice(eventStart).reverse().find(event => event && event.urlPath && event.urlPath.includes('/oauth2/v3/token')) || null;
  if (cdpEvent) {
    report.direct_auth.oauth_cdp_event_count = (report.direct_auth.oauth_cdp_event_count || 0) + 1;
    report.direct_auth.oauth_cdp_last_status = cdpEvent.status;
    report.direct_auth.cdp_mfa_process_id_header_available = !!cdpEvent.mfaProcessIdHeader;
    report.direct_auth.cdp_mfa_process_id_header_len = cdpEvent.mfaProcessIdHeaderLen || 0;
    if (cdpEvent.mfaProcessIdHeader && !result.mfaProcessIdHeader) {
      result.mfaProcessIdHeader = cdpEvent.mfaProcessIdHeader;
      result.mfaProcessIdHeaderSource = 'cdp';
    }
  }
  result.transport = 'browser';
  return result;
}

async function requestOauthToken(creds, report, mfaProcessId = '') {
  if (AUTH_TRANSPORT === 'browser') {
    const parsed = await browserOauthToken(creds, report, mfaProcessId);
    report.direct_auth.password_token_status = parsed.status;
    report.direct_auth.auth_transport = 'browser';
    return parsed;
  }
  const headers = oauthHeaders(mfaProcessId);
  const body = formBody({
    grant_type: 'password',
    username: creds.email,
    password: creds.password,
    environment_name: ENVIRONMENT_NAME,
  });
  const resp = await fetch(`${new URL(PORTAL_URL).origin}/mycitadelservicing/webapi/oauth2/v3/token`, {
    method: 'POST',
    headers,
    body,
  });
  const parsed = await readResponse(resp);
  parsed.requestShape = oauthRequestShape(headers, String(body));
  report.direct_auth.password_token_status = parsed.status;
  report.direct_auth.auth_transport = 'node';
  return parsed;
}

async function requestClientToken(report) {
  const resp = await fetch(`${new URL(PORTAL_URL).origin}/mycitadelservicing/webapi/oauth2/v3/token`, {
    method: 'POST',
    headers: oauthHeaders(),
    body: formBody({ grant_type: 'client_credentials', environment_name: ENVIRONMENT_NAME }),
  });
  const parsed = await readResponse(resp);
  report.direct_auth.client_token_status = parsed.status;
  return parsed;
}

function findFeatureProperty(features, featureName, propertyName) {
  const feature = features && features[featureName];
  const props = feature && (feature.properties || feature.Properties || feature);
  return props && (props[propertyName] || props[propertyName.charAt(0).toUpperCase() + propertyName.slice(1)]);
}

async function fetchInstitutionFeatures(clientToken, report) {
  const resp = await fetch(`${API_BASE}/sd/preauth/config/v1/institution/config?featuresVersion=5`, {
    headers: {
      accept: 'application/json',
      authorization: `Bearer ${clientToken}`,
      origin: new URL(PORTAL_URL).origin,
      referer: `${new URL(PORTAL_URL).origin}/mycitadelservicing/`,
      'x-correlation-id': randomUUID(),
    },
  });
  const parsed = await readResponse(resp);
  report.direct_auth.features_status = parsed.status;
  return parsed.ok && parsed.json ? parsed.json : null;
}

async function getFreshRecaptchaTokenViaCdp(siteKey, providerJsUrl, report, attempt = 0) {
  if (!siteKey || process.env.CITADEL_FRESH_RECAPTCHA === '0') return '';
  let browser = null;
  try {
    browser = await createCdpPage(PORTAL_URL, report, 'recaptcha');
    const scriptUrl = (providerJsUrl || 'https://www.google.com/recaptcha/enterprise.js?render={webSiteKey}').replace('{webSiteKey}', siteKey);
    report.direct_auth.recaptcha_script_url_host = (() => {
      try { return new URL(scriptUrl).host; } catch { return null; }
    })();
    const expression = `
      (async () => {
        const waitMs = ${JSON.stringify(RECAPTCHA_WAIT_MS)};
        const actions = ${JSON.stringify(RECAPTCHA_ACTIONS)};
        let stage = 'start';
        let token = '';
        let tokenAction = '';
        let error = '';
        const actionErrors = [];
        const withTimeout = (promise, label) => Promise.race([
          promise,
          new Promise((_, reject) => setTimeout(() => reject(new Error(label + ' timed out')), waitMs))
        ]);
        const waitForDocumentHead = () => new Promise(resolve => {
          if (document.head && document.readyState !== 'loading') return resolve();
          const done = () => {
            if (document.head) {
              window.removeEventListener('DOMContentLoaded', done);
              window.removeEventListener('load', done);
              resolve();
            }
          };
          window.addEventListener('DOMContentLoaded', done, { once: true });
          window.addEventListener('load', done, { once: true });
          const timer = setInterval(() => {
            if (!document.head) return;
            clearInterval(timer);
            done();
          }, 100);
        });
        try {
          const siteKey = ${JSON.stringify(siteKey)};
          const scriptUrl = ${JSON.stringify(scriptUrl)};
          let page = {};
          stage = 'page_head';
          await withTimeout(waitForDocumentHead(), 'citadel page head');
          if (document.readyState !== 'complete') {
            stage = 'page_load';
            await withTimeout(new Promise(resolve => {
              if (document.readyState === 'complete') return resolve();
              window.addEventListener('load', resolve, { once: true });
            }), 'citadel page load');
          }
          page = {
            url: location.href,
            readyState: document.readyState,
            hasHead: !!document.head,
            existingScript: [...document.scripts].some(s => s.src === scriptUrl),
            grecaptchaBefore: !!(window.grecaptcha && window.grecaptcha.enterprise)
          };
          if (!window.grecaptcha || !window.grecaptcha.enterprise) {
            stage = 'script_load';
            await withTimeout(new Promise((resolve, reject) => {
              const existing = [...document.scripts].find(s => s.src === scriptUrl);
              if (existing) return resolve();
              const s = document.createElement('script');
              s.src = scriptUrl;
              s.async = true;
              s.onload = resolve;
              s.onerror = () => reject(new Error('recaptcha script load failed'));
              document.head.appendChild(s);
            }), 'recaptcha script load');
          }
          stage = 'ready';
          await withTimeout(new Promise(resolve => window.grecaptcha.enterprise.ready(resolve)), 'recaptcha ready');
          for (const action of actions) {
            stage = 'execute:' + action;
            try {
              const candidate = await withTimeout(
                window.grecaptcha.enterprise.execute(siteKey, { action }),
                'recaptcha execute ' + action
              );
              if (candidate) {
                token = String(candidate);
                tokenAction = action;
                break;
              }
              actionErrors.push({ action, error: 'empty token' });
            } catch (e) {
              actionErrors.push({ action, error: String(e && e.message || e).slice(0, 120) });
            }
          }
          if (!token && actionErrors.length) error = actionErrors[actionErrors.length - 1].error;
          return JSON.stringify({
            token,
            tokenAction,
            tokenType: token ? 'string' : 'empty',
            tokenLength: token ? String(token).length : 0,
            mobileSourceId: localStorage.getItem('x-mobile-source-id') || '',
            grecaptchaAfter: !!(window.grecaptcha && window.grecaptcha.enterprise),
            stage,
            error,
            actionErrors,
            page
          });
        } catch (e) {
          error = String(e && e.message || e);
          return JSON.stringify({
            token: '',
            tokenAction,
            tokenType: 'error',
            tokenLength: 0,
            mobileSourceId: localStorage.getItem('x-mobile-source-id') || '',
            grecaptchaAfter: !!(window.grecaptcha && window.grecaptcha.enterprise),
            stage,
            error,
            actionErrors,
            page: {
              url: location.href,
              readyState: document.readyState,
              hasHead: !!document.head,
              existingScript: [...document.scripts].some(s => s.src === ${JSON.stringify(scriptUrl)}),
              grecaptchaBefore: !!(window.grecaptcha && window.grecaptcha.enterprise)
            }
          });
        }
      })()
    `;
    const rawValue = await browser.evalExpr(expression);
    let value = null;
    if (typeof rawValue === 'string' && rawValue) {
      try { value = JSON.parse(rawValue); } catch (e) {
        report.direct_auth.recaptcha_eval_parse_error = e.message;
      }
    }
    report.direct_auth.recaptcha_eval_returned_value = !!value;
    const token = value && value.token;
    const mobileSourceId = value && value.mobileSourceId;
    if (!process.env.CITADEL_MOBILE_SOURCE_ID && mobileSourceId) currentMobileSourceId = String(mobileSourceId);
    report.direct_auth.recaptcha_eval_page = value && value.page ? {
      ready_state: value.page.readyState || null,
      url_path: (() => {
        try { return new URL(value.page.url || '').pathname; } catch { return null; }
      })(),
      has_head: !!value.page.hasHead,
      existing_script: !!value.page.existingScript,
      grecaptcha_before: !!value.page.grecaptchaBefore,
    } : null;
    report.direct_auth.recaptcha_grecaptcha_after_available = !!(value && value.grecaptchaAfter);
    report.direct_auth.recaptcha_eval_stage = value && value.stage || null;
    report.direct_auth.recaptcha_eval_error = value && value.error ? String(value.error).slice(0, 160) : null;
    report.direct_auth.recaptcha_token_action = value && value.tokenAction || null;
    report.direct_auth.recaptcha_action_count = RECAPTCHA_ACTIONS.length;
    report.direct_auth.recaptcha_action_errors = Array.isArray(value && value.actionErrors)
      ? value.actionErrors.map(item => ({
        action: item && item.action || null,
        error: item && item.error ? String(item.error).slice(0, 120) : null,
      }))
      : [];
    report.direct_auth.recaptcha_execute_token_type = value && value.tokenType || null;
    report.direct_auth.recaptcha_execute_token_length = value && value.tokenLength || 0;
    report.direct_auth.fresh_recaptcha_token_available = !!token;
    report.direct_auth.fresh_recaptcha_token_length = token ? String(token).length : 0;
    report.direct_auth.browser_mobile_source_id_available = !!mobileSourceId;
    report.direct_auth.browser_mobile_source_id_used = !!(!process.env.CITADEL_MOBILE_SOURCE_ID && mobileSourceId);
    return token ? String(token) : '';
  } catch (e) {
    report.direct_auth.fresh_recaptcha_error = e.message;
    if (attempt < 1 && /Execution context was destroyed|Cannot find context|context.*destroyed/i.test(String(e.message || e))) {
      report.direct_auth.recaptcha_retry_count = (report.direct_auth.recaptcha_retry_count || 0) + 1;
      if (browser) {
        await browser.close();
        browser = null;
      }
      return getFreshRecaptchaTokenViaCdp(siteKey, providerJsUrl, report, attempt + 1);
    }
    return '';
  } finally {
    if (browser) await browser.close();
  }
}

async function prepareFreshMfaSource(report) {
  const client = await requestClientToken(report);
  const clientToken = client.ok && client.json ? client.json.access_token : '';
  if (!clientToken) {
    report.direct_auth.fresh_mfa_source_status = 'client_token_failed';
    return '';
  }
  const featureConfig = await fetchInstitutionFeatures(clientToken, report);
  const features = featureConfig && (featureConfig.Features || featureConfig.features);
  const siteKey = findFeatureProperty(features, 'GRECAPTCHA', 'webSiteKey');
  const provider = findFeatureProperty(features, 'GRECAPTCHA', 'xMfaProvider');
  const providerJsUrl = findFeatureProperty(features, 'GRECAPTCHA', 'mfaProviderJSURL');
  report.direct_auth.grecaptcha_site_key_available = !!siteKey;
  report.direct_auth.grecaptcha_provider = provider || null;
  if (!currentMfaProvider && provider) currentMfaProvider = provider;
  const token = await getFreshRecaptchaTokenViaCdp(siteKey, providerJsUrl, report);
  if (token) {
    currentMfaSourceId = token;
    report.direct_auth.fresh_mfa_source_status = 'ready';
    return clientToken;
  }
  report.direct_auth.fresh_mfa_source_status = 'unavailable';
  return clientToken;
}

async function requestEmailOtp(requestUuid, clientToken, report, sendViaType = 'email') {
  const headers = browserHeaders('application/json');
  if (clientToken && process.env.CITADEL_OTP_AUTH_BEARER === '1') headers.authorization = `Bearer ${clientToken}`;
  const body = { RequestUUID: requestUuid, SendViaType: sendViaType };
  const shape = otpRequestShape(headers, body);
  report.direct_auth.otp_request_shape = shape;
  report.direct_auth.otp_request_shape_matches_har_success = otpRequestShapeMatchesHar(shape, report.credential_diagnostics);
  const resp = await fetch(`${API_BASE}/sd/preauth/ssp/v1/process/otps`, {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
  });
  const parsed = await readResponse(resp);
  report.direct_auth.otp_request_status = parsed.status;
  report.direct_auth.otp_send_via_type = sendViaType;
  return parsed;
}

async function verifyEmailOtp(requestUuid, otp, clientToken, report) {
  const headers = browserHeaders('application/json');
  if (clientToken && process.env.CITADEL_OTP_AUTH_BEARER === '1') headers.authorization = `Bearer ${clientToken}`;
  const resp = await fetch(`${API_BASE}/sd/preauth/ssp/v1/process/otps/${requestUuid}/verify`, {
    method: 'POST',
    headers,
    body: JSON.stringify({ OneTimePin: otp }),
  });
  const parsed = await readResponse(resp);
  report.direct_auth.otp_verify_status = parsed.status;
  return parsed;
}

async function directPasswordAuth(creds, report) {
  report.direct_auth = { attempted: true, environment_name: ENVIRONMENT_NAME };
  report.direct_auth.har_auth_diagnostics_enabled = ALLOW_HAR_AUTH_DIAGNOSTICS;
  report.direct_auth.har_browser_header_defaults_enabled = USE_HAR_BROWSER_HEADER_DEFAULTS;
  report.direct_auth.har_header_defaults_loaded = Object.keys(HAR_HEADER_DEFAULTS).length > 0;
  report.direct_auth.mobile_source_id_available = !!currentMobileSourceId;
  report.direct_auth.mfa_source_id_available = !!currentMfaSourceId;
  report.direct_auth.mfa_provider_available = !!currentMfaProvider;
  report.direct_auth.mfa_provider = currentMfaProvider || null;
  report.direct_auth.browser_header_defaults_available = !!(USER_AGENT && ACCEPT_LANGUAGE);
  let preparedClientToken = '';
  if (!process.env.CITADEL_MFA_SOURCE_ID) {
    preparedClientToken = await prepareFreshMfaSource(report);
    report.direct_auth.mfa_source_id_available = !!currentMfaSourceId;
    report.direct_auth.mfa_provider_available = !!currentMfaProvider;
    report.direct_auth.mfa_provider = currentMfaProvider || null;
  }
  if (!currentMfaSourceId) {
    report.direct_auth.status = 'mfa_source_unavailable';
    return '';
  }
  const first = await requestOauthToken(creds, report);
  report.direct_auth.password_token_request_shape = first.requestShape || null;
  const firstShapeComparison = oauthShapeHarComparison(first.requestShape, report.credential_diagnostics);
  report.direct_auth.password_token_request_shape_matches_har_success = firstShapeComparison.success;
  report.direct_auth.password_token_request_shape_matches_har_failure = firstShapeComparison.failure;
  report.direct_auth.password_token_request_shape_matched_har_statuses = firstShapeComparison.matched_statuses;
  report.direct_auth.password_token_request_structure_matches_har_success = firstShapeComparison.structure_success;
  report.direct_auth.password_token_request_structure_matches_har_failure = firstShapeComparison.structure_failure;
  report.direct_auth.password_token_request_structure_matched_har_statuses = firstShapeComparison.structure_matched_statuses;
  report.direct_auth.password_token_request_structure_matches_har_initial_mfa_challenge = firstShapeComparison.structure_initial_mfa_challenge;
  if (first.ok && first.json && first.json.access_token) {
    report.direct_auth.status = 'token_acquired';
    return first.json.access_token;
  }

  const mfaPayload = first.json || {};
  report.direct_auth.first_error = directAuthErrorSummary(mfaPayload);
  report.direct_auth.mfa_process_id_header_available = !!first.mfaProcessIdHeader;
  const mfaDetail = extractMfaDetail(mfaPayload);
  report.direct_auth.mfa_detail_available = !!mfaDetail;
  if (mfaDetail) {
    report.direct_auth.mfa_detail_request_uuid_available = !!mfaDetail.request_uuid;
    report.direct_auth.mfa_detail_masked_email_available = mfaDetail.masked_email_available;
    report.direct_auth.mfa_detail_masked_phone_available = mfaDetail.masked_phone_available;
    report.direct_auth.mfa_detail_send_via_types = mfaDetail.send_via_types;
  }
  let requestUuid = '';
  let requestUuidSource = '';
  if (first.mfaProcessIdHeader) {
    requestUuid = first.mfaProcessIdHeader;
    requestUuidSource = first.mfaProcessIdHeaderSource || 'response_header';
  } else if (mfaDetail && mfaDetail.request_uuid) {
    requestUuid = mfaDetail.request_uuid;
    requestUuidSource = 'mfa_detail';
  } else {
    requestUuid = findFirstUuid(mfaPayload);
    requestUuidSource = requestUuid ? 'payload_scan' : '';
  }
  if (!requestUuid) {
    const firstErrorCodes = Array.isArray(report.direct_auth.first_error && report.direct_auth.first_error.error_message_codes)
      ? report.direct_auth.first_error.error_message_codes
      : [];
    const harMfaProcessIdHeaderCount = Number(report.credential_diagnostics && report.credential_diagnostics.har_mfa_process_id_header_count || 0);
    const canSynthesizeMfaRequestUuid = process.env.CITADEL_SYNTHESIZE_MFA_REQUEST_UUID !== '0'
      && firstErrorCodes.includes('20000')
      && report.direct_auth.password_token_request_structure_matches_har_initial_mfa_challenge
      && !!(report.credential_diagnostics && report.credential_diagnostics.har_successful_password_token_match);
    report.direct_auth.no_mfa_handoff_after_password = firstErrorCodes.includes('20000')
      && !first.mfaProcessIdHeader
      && !mfaDetail
      && harMfaProcessIdHeaderCount > 0;
    if (report.direct_auth.no_mfa_handoff_after_password) {
      report.direct_auth.no_mfa_handoff_reason = 'password_token_rejected_without_mfa_process_id';
      if (report.direct_auth.password_token_request_structure_matches_har_initial_mfa_challenge) {
        report.direct_auth.no_mfa_handoff_reason = 'initial_mfa_challenge_shape_matched_but_process_id_missing';
      }
    }
    report.direct_auth.mfa_request_uuid_synthesis_enabled = process.env.CITADEL_SYNTHESIZE_MFA_REQUEST_UUID !== '0';
    report.direct_auth.mfa_request_uuid_synthesis_candidate = canSynthesizeMfaRequestUuid;
    if (canSynthesizeMfaRequestUuid) {
      requestUuid = randomUUID();
      requestUuidSource = 'generated_initial_mfa_challenge';
      report.direct_auth.mfa_request_uuid_generated = true;
    } else {
      applyCredentialStateDriftAssessment(report.direct_auth, report, firstErrorCodes.includes('20000'));
      report.direct_auth.status = firstErrorCodes.includes('20000') ? 'credential_rejected_before_mfa' : 'password_token_failed';
      return '';
    }
  }
  report.direct_auth.mfa_request_uuid_available = true;
  report.direct_auth.mfa_request_uuid_source = requestUuidSource;

  const clientToken = preparedClientToken || (await requestClientToken(report)).json?.access_token || '';
  if (!clientToken) {
    report.direct_auth.status = 'client_token_failed';
    return '';
  }

  const otpRequest = await requestEmailOtp(requestUuid, clientToken, report, chooseOtpSendViaType(mfaDetail));
  report.direct_auth.otp_requested = otpRequest.ok;
  if (!otpRequest.ok) {
    report.direct_auth.status = 'otp_request_failed';
    report.direct_auth.otp_request_error = directAuthErrorSummary(otpRequest.json || {});
    return '';
  }

  const otp = await waitForOtp(report);
  if (!otp) {
    report.direct_auth.status = 'otp_required';
    return '';
  }
  const verify = await verifyEmailOtp(requestUuid, otp, clientToken, report);
  report.direct_auth.otp_verified = verify.ok || verify.status === 204;
  if (!report.direct_auth.otp_verified) {
    report.direct_auth.status = 'otp_verify_failed';
    report.direct_auth.otp_verify_error = directAuthErrorSummary(verify.json || {});
    return '';
  }

  const finalToken = await requestOauthToken(creds, report, requestUuid);
  report.direct_auth.final_password_token_request_shape = finalToken.requestShape || null;
  const finalShapeComparison = oauthShapeHarComparison(finalToken.requestShape, report.credential_diagnostics);
  report.direct_auth.final_password_token_request_shape_matches_har_success = finalShapeComparison.success;
  report.direct_auth.final_password_token_request_shape_matches_har_failure = finalShapeComparison.failure;
  report.direct_auth.final_password_token_request_shape_matched_har_statuses = finalShapeComparison.matched_statuses;
  report.direct_auth.final_password_token_request_structure_matches_har_success = finalShapeComparison.structure_success;
  report.direct_auth.final_password_token_request_structure_matches_har_failure = finalShapeComparison.structure_failure;
  report.direct_auth.final_password_token_request_structure_matched_har_statuses = finalShapeComparison.structure_matched_statuses;
  if (finalToken.ok && finalToken.json && finalToken.json.access_token) {
    report.direct_auth.status = 'token_acquired_after_otp';
    return finalToken.json.access_token;
  }
  report.direct_auth.status = 'final_password_token_failed';
  report.direct_auth.final_error = directAuthErrorSummary(finalToken.json || {});
  return '';
}

async function replayHarStatements(report, existingNames) {
  report.har_workflow_diagnostics = harWorkflowDiagnostics();
  report.har_replay = { attempted: true };
  if (!report.har_workflow_diagnostics.har_path_exists) {
    report.status = 'har_unavailable';
    report.errors.push('Citadel HAR path is unavailable');
    return 2;
  }
  if (!report.har_workflow_diagnostics.can_replay_documents) {
    if (report.har_workflow_diagnostics.replay_blocker === 'target_month_statement_unavailable') {
      report.status = 'target_month_missing';
      report.target_month = report.har_workflow_diagnostics.target_month || report.target_month;
      report.errors.push(`Citadel target-month statement unavailable: ${report.target_month}`);
      return 1;
    }
    report.status = 'har_unavailable';
    const blocker = report.har_workflow_diagnostics.replay_blocker || 'no_replayable_document_payloads';
    report.errors.push(`Citadel HAR cannot replay statements: ${blocker}`);
    return 2;
  }

  let har;
  try {
    har = JSON.parse(fs.readFileSync(HAR_PATH, 'utf8'));
  } catch (e) {
    report.status = 'har_unavailable';
    report.errors.push(`Citadel HAR parse failed: ${e.message}`);
    return 2;
  }

  const entries = (((har || {}).log || {}).entries || []);
  const eStatements = [];
  const docs = [];
  const details = new Map();
  const directPdfByDocId = new Map();
  const directPdfs = [];
  for (const entry of entries) {
    const request = entry.request || {};
    const response = entry.response || {};
    if (response.status !== 200) continue;
    const url = String(request.url || '');
    let pathName = '';
    try { pathName = new URL(url).pathname; } catch { pathName = url; }
    const directPdf = harPdfBuffer(entry);
    if (directPdf) {
      const docPrefix = `/lsd/api/v1/my/loans/${LOAN_ORG}/${LOAN_ID}/docs/`;
      if (pathName.includes(docPrefix)) {
        const docId = decodeURIComponent(pathName.slice(pathName.indexOf(docPrefix) + docPrefix.length).split('/')[0]);
        if (docId && !directPdfByDocId.has(docId)) directPdfByDocId.set(docId, directPdf);
      }
      directPdfs.push({ pathName, buffer: directPdf, used: false });
    }
    const json = harJson(entry);
    if (!json) continue;
    if (pathName.includes(`/sd/loan-maint/v2/my/loans/${LOAN_ORG}/${LOAN_ID}/eStatements`)) {
      eStatements.push(...unwrapArray(json, ['statements', 'Statements', 'eStatements', 'EStatements', 'data', 'Data']));
    } else if (pathName.endsWith(`/lsd/api/v1/my/loans/${LOAN_ORG}/${LOAN_ID}/docs`)) {
      docs.push(...unwrapArray(json, ['Documents', 'documents', 'docs', 'Docs', 'data', 'Data']));
    } else if (pathName.includes(`/lsd/api/v1/my/loans/${LOAN_ORG}/${LOAN_ID}/docs/`)) {
      details.set(pathName.split('/').pop(), json);
    }
  }

  report.eStatements_count = eStatements.length;
  report.docs_count = docs.length;
  const statementDocs = docs.filter(isStatementDocument);
  report.statement_doc_count = statementDocs.length;
  const candidates = [
    ...eStatements.map(doc => ({ source: 'har:eStatements', doc })),
    ...statementDocs.map(doc => ({ source: 'har:docs', doc })),
  ];
  report.har_replay.candidate_count = candidates.length;

  for (const candidate of candidates) {
    const doc = candidate.doc;
    const docId = String(docField(doc, 'id', 'Id', 'ID', 'documentId', 'DocumentId', 'DocumentID', 'docId', 'DocId', 'DocumentIdentifier') || '');
    const detail = docId ? (details.get(docId) || {}) : {};
    let docName = docField(doc, 'name', 'Name', 'documentName', 'DocumentName', 'title', 'Title', 'type', 'Type', 'DocumentType') || '';
    let docDate = docField(doc, 'date', 'Date', 'statementDate', 'StatementDate', 'createdDate', 'CreatedDate', 'postedDate', 'PostedDate', 'DocumentDate', 'documentDate', 'DocumentCreationDate', 'documentCreationDate', 'creationDate', 'CreationDate') || '';
    docName = docName || docField(detail, 'name', 'Name', 'documentName', 'DocumentName', 'title', 'Title') || '';
    docDate = docDate || docField(detail, 'date', 'Date', 'statementDate', 'StatementDate', 'createdDate', 'CreatedDate', 'postedDate', 'PostedDate', 'DocumentDate', 'documentDate', 'DocumentCreationDate', 'documentCreationDate', 'creationDate', 'CreationDate') || '';
    const pdfBuffer = pdfBufferFromBase64(pdfBase64FromJson(doc) || pdfBase64FromJson(detail))
      || (docId ? directPdfByDocId.get(docId) : null)
      || (() => {
        const available = directPdfs.find(item => !item.used);
        if (!available) return null;
        available.used = true;
        return available.buffer;
      })();

    const dateStr = statementDateFromDoc(docName, docDate);
    if (!dateStr) {
      report.skipped_files.push({ name: docName, id: docId, reason: 'no_date_found', source: candidate.source });
      continue;
    }
    const filePath = statementPathForDate(dateStr);
    if (!filePath) {
      report.skipped_files.push({ name: docName, id: docId, reason: 'invalid_date', source: candidate.source });
      continue;
    }
    const fileName = path.basename(filePath);
    const statementMonth = dateStr.slice(0, 7);
    if (existingNames.has(fileName)) {
      report.skipped_files.push({ name: fileName, id: docId, reason: 'already_exists', source: candidate.source, statement_date: dateStr, statement_month: statementMonth });
      continue;
    }
    if (!pdfBuffer) {
      report.skipped_files.push({ name: fileName, id: docId, reason: 'no_embedded_pdf_payload', source: candidate.source, statement_date: dateStr, statement_month: statementMonth });
      continue;
    }
    fs.writeFileSync(filePath, pdfBuffer);
    report.downloaded_files.push({ name: fileName, id: docId, size: pdfBuffer.length, source: candidate.source, statement_date: dateStr, statement_month: statementMonth });
    existingNames.add(fileName);
  }

  refreshTargetMonthSummary(report);
  if (!report.target_month_statement_available) {
    report.status = 'target_month_missing';
    report.errors.push(`Citadel target-month statement unavailable: ${report.target_month}`);
    return 1;
  }
  report.status = report.errors.length > 0 ? 'partial' : 'ok';
  return report.status === 'ok' ? 0 : 1;
}

function refreshTargetMonthSummary(report) {
  const targetMonth = report.target_month || currentTargetMonth();
  report.target_month = targetMonth;
  report.existing_target_month_files = (report.existing_files || []).filter(name => statementMonthFromName(path.basename(name)) === targetMonth);
  report.downloaded_target_month_files = (report.downloaded_files || []).filter(item => item && item.statement_month === targetMonth);
  report.skipped_target_month_files = (report.skipped_files || []).filter(item => item && item.statement_month === targetMonth);
  report.target_month_existing_count = report.existing_target_month_files.length;
  report.target_month_downloaded_count = report.downloaded_target_month_files.length;
  report.target_month_skipped_count = report.skipped_target_month_files.length;
  report.target_month_statement_available = (
    report.target_month_existing_count + report.target_month_downloaded_count
  ) > 0;
}

function shouldSkipBecauseTargetMonthExists(report) {
  if (LOGIN_MODE === 'har') return false;
  if (process.env.CITADEL_SKIP_IF_TARGET_MONTH_EXISTS === '0') return false;
  return report.target_month_statement_available === true
    && Number(report.target_month_existing_count || 0) > 0;
}

async function main() {
  fs.mkdirSync(REPORTS, { recursive: true });
  fs.mkdirSync(STATEMENT_DIR, { recursive: true });
  const report = {
    job: 'citadel-statements-download',
    started_at: new Date().toISOString(),
    status: 'unknown',
    loan_org: LOAN_ORG,
    loan_id: LOAN_ID,
    existing_files: [],
    statement_dir: STATEMENT_DIR,
    statement_dir_candidates: STATEMENT_DIR_CANDIDATES,
    downloaded_files: [],
    skipped_files: [],
    errors: [],
    warnings: [],
    api_responses: {},
    login_mode: LOGIN_MODE,
    target_month: currentTargetMonth(),
    existing_target_month_files: [],
    downloaded_target_month_files: [],
    skipped_target_month_files: [],
    target_month_existing_count: 0,
    target_month_downloaded_count: 0,
    target_month_skipped_count: 0,
    target_month_statement_available: false,
  };
  report.har_workflow_diagnostics = harWorkflowDiagnostics();

  // List existing statement PDFs in canonical and legacy 90 Madison folders.
  report.existing_files = listExistingStatementFiles();
  const existingNames = new Set(report.existing_files.map(f => path.basename(f)));
  refreshTargetMonthSummary(report);

  if (shouldSkipBecauseTargetMonthExists(report)) {
    report.status = 'ok';
    report.idempotent_skip = true;
    report.idempotent_skip_reason = 'target_month_statement_already_available';
    report.ended_at = new Date().toISOString();
    writeReport(report);
    console.error(`[citadel] Target month ${report.target_month} already available locally; skipping Citadel auth/download.`);
    process.exit(0);
  }

  if (LOGIN_MODE === 'har') {
    const rc = await replayHarStatements(report, existingNames);
    refreshTargetMonthSummary(report);
    report.ended_at = new Date().toISOString();
    writeReport(report);
    console.error(`[citadel] HAR replay ${report.status}; downloaded ${report.downloaded_files.length}, skipped ${report.skipped_files.length}, errors ${report.errors.length}`);
    process.exit(rc);
  }

  let creds = null;
  try {
    const item = getCredItem();
    const itemSummary = credentialItemSummary(item);
    const login = item && item.login || {};
    const email = login.username || getCred('username');
    const password = login.password || getCred('password');
    report.credential_item_summary = itemSummary;
    if (email && password) {
      creds = { email, password };
      report.credentials_available = true;
      report.credential_diagnostics = credentialDiagnostics(creds, itemSummary);
    } else {
      report.credentials_available = false;
      report.warnings.push('Citadel credentials unavailable from noninteractive Bitwarden session');
    }
  } catch (e) {
    report.credentials_available = false;
    report.errors.push('Credential retrieval failed: '+ e.message);
  }

  let directAuthToken = '';
  let directAuthIncompleteStatus = '';
  let directAuthReachedVerifiedOtpRequest = false;
  if (creds && LOGIN_MODE === 'direct') {
    try {
      directAuthToken = await directPasswordAuth(creds, report);
      report.direct_auth.token_available = !!directAuthToken;
    } catch (e) {
      report.direct_auth = report.direct_auth || { attempted: true };
      report.direct_auth.status = 'error';
      report.direct_auth.error = e.message;
    }
    if (!directAuthToken) {
      directAuthIncompleteStatus = (report.direct_auth || {}).status || 'unknown';
      directAuthReachedVerifiedOtpRequest = directAuthIncompleteStatus === 'otp_request_failed'
        && !!(report.direct_auth && report.direct_auth.otp_request_shape_matches_har_success);
      report.direct_auth.browser_fallback_attempted = true;
      report.warnings.push('Citadel direct authentication did not complete; scanning CDP tabs for an authenticated Citadel session');
    }
  }

  let sessionId = null;
  let targetId = null;
  try {
    const version = await (await fetch(VERSION_URL)).json();
    const ws = new WebSocket(version.webSocketDebuggerUrl);
	    let id = 0;
	    const pending = new Map();
	    const sessions = new Map();
	    const responseBodies = new Map();
	    const oauthRequestEvents = new Map();
	    const oauthRequestPostData = new Map();

	    function updateOauthRequestShape(event, headers, postData) {
	      event.request_shape = oauthRequestShape(headers || {}, postData || '');
	      const comparison = oauthShapeHarComparison(event.request_shape, report.credential_diagnostics);
	      event.request_shape_matches_har_success = comparison.success;
	      event.request_shape_matches_har_failure = comparison.failure;
	      event.request_shape_matched_har_statuses = comparison.matched_statuses;
	      event.request_structure_matches_har_success = comparison.structure_success;
	      event.request_structure_matches_har_failure = comparison.structure_failure;
	      event.request_structure_matched_har_statuses = comparison.structure_matched_statuses;
	    }

    function send(method, params = {}, sessId) {
      const msg = { id: ++id, method, params };
      if (sessId) msg.sessionId = sessId;
      ws.send(JSON.stringify(msg));
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          pending.delete(msg.id);
          reject(new Error(`CDP command timed out: ${method}`));
        }, TIMEOUT_MS);
        pending.set(msg.id, {
          resolve: value => {
            clearTimeout(timer);
            resolve(value);
          },
          reject: error => {
            clearTimeout(timer);
            reject(error);
          },
        });
      });
    }

    ws.onmessage = ev => {
      const msg = JSON.parse(ev.data);
      if (msg.id) {
        const p = pending.get(msg.id);
        if (p) {
          pending.delete(msg.id);
          if (msg.error) p.reject(new Error(JSON.stringify(msg.error)));
          else p.resolve(msg.result);
        }
        return;
      }
	      if (msg.method === 'Target.attachedToTarget') {
	        sessions.set(msg.params.targetInfo.targetId, msg.params.sessionId);
	      }
	      if (msg.sessionId === sessionId && msg.method === 'Network.requestWillBeSent') {
	        const params = msg.params || {};
	        const request = params.request || {};
	        const url = request.url || '';
	        if (url.includes('/oauth2/v3/token')) {
	          report.oauth_network_events = report.oauth_network_events || [];
	          const event = oauthRequestEvents.get(params.requestId) || {
	            request_id: params.requestId,
	            url_path: (() => {
	              try { return new URL(url).pathname; } catch { return url; }
	            })(),
	          };
	          event.method = request.method || null;
	          oauthRequestPostData.set(params.requestId, request.postData || '');
	          updateOauthRequestShape(event, request.headers || {}, request.postData || '');
	          if (!oauthRequestEvents.has(params.requestId)) report.oauth_network_events.push(event);
	          oauthRequestEvents.set(params.requestId, event);
	        }
	      }
	      if (msg.sessionId === sessionId && msg.method === 'Network.requestWillBeSentExtraInfo') {
	        const params = msg.params || {};
	        const event = oauthRequestEvents.get(params.requestId);
	        if (event) {
	          event.request_extra_info_available = true;
	          updateOauthRequestShape(event, params.headers || {}, oauthRequestPostData.get(params.requestId) || '');
	        }
	      }
	      if (msg.sessionId === sessionId && msg.method === 'Network.responseReceived') {
	        const params = msg.params || {};
	        const response = params.response || {};
	        const url = response.url || '';
	        if (url.includes('/oauth2/v3/token')) {
	          report.oauth_network_events = report.oauth_network_events || [];
	          const event = oauthRequestEvents.get(params.requestId) || {
	            request_id: params.requestId,
	            url_path: (() => {
	              try { return new URL(url).pathname; } catch { return url; }
	            })(),
	          };
	          event.status = response.status;
	          event.mime_type = response.mimeType;
	          event.from_disk_cache = !!response.fromDiskCache;
	          if (!oauthRequestEvents.has(params.requestId)) {
	            report.oauth_network_events.push(event);
	            oauthRequestEvents.set(params.requestId, event);
	          }
	          responseBodies.set(params.requestId, event);
	        }
	      }
      if (msg.sessionId === sessionId && msg.method === 'Network.loadingFinished') {
        const event = responseBodies.get((msg.params || {}).requestId);
        if (event) {
          send('Network.getResponseBody', { requestId: event.request_id }, sessionId)
            .then(bodyResult => {
              const text = bodyResult && bodyResult.body || '';
              let json = null;
              try { json = text ? JSON.parse(text) : null; } catch {}
              event.body_len = text.length;
              event.error_summary = directAuthErrorSummary(json || {});
            })
            .catch(error => {
              event.body_error = String(error && error.message || error).slice(0, 160);
            });
        }
      }
    };

    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('CDP websocket open timed out')), TIMEOUT_MS);
      ws.onopen = () => {
        clearTimeout(timer);
        resolve();
      };
      ws.onerror = error => {
        clearTimeout(timer);
        reject(error);
      };
    });
    await send('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: false, flatten: true });

    function apiFetchOptionsLiteral(includeAuth = false) {
      const headers = { accept: 'application/json', 'x-correlation-id': randomUUID() };
      if (currentMobileSourceId) headers['x-mobile-source-id'] = currentMobileSourceId;
      if (includeAuth && directAuthToken) headers.authorization = `Bearer ${directAuthToken}`;
      return JSON.stringify({ credentials: 'include', headers });
    }

    function apiFetchOptionsBrowserExpression(includeAuth = false, includeSnapshot = false) {
      return `(() => {
        ${browserStorageAuthHelperSource()}
        const auth = __citadelStorageAuth();
        const options = ${apiFetchOptionsLiteral(includeAuth)};
        options.headers = options.headers || {};
        if (auth.mobileSourceId && !options.headers['x-mobile-source-id']) {
          options.headers['x-mobile-source-id'] = auth.mobileSourceId;
        }
        if (auth.bearerToken && !options.headers.authorization) {
          options.headers.authorization = 'Bearer ' + auth.bearerToken;
        }
        if (${includeSnapshot ? 'true' : 'false'}) {
          options.__citadelAuthSnapshot = {
            bearer_token_available: !!auth.bearerToken,
            mobile_source_id_available: !!auth.mobileSourceId
          };
        }
        return options;
      })()`;
    }

    async function enablePageSession(sessId, bringToFront = false) {
      await send('Page.enable', {}, sessId);
      await send('Runtime.enable', {}, sessId);
      await send('Network.enable', {}, sessId);
      if (bringToFront) await send('Page.bringToFront', {}, sessId).catch(() => null);
    }

    async function createFreshCitadelTarget(source) {
      const result = await send('Target.createTarget', { url: PORTAL_URL });
      targetId = result.targetId;
      await send('Target.activateTarget', { targetId }).catch(() => null);
      sessionId = null;
      for (let i = 0; i < 120; i++) {
        sessionId = sessions.get(targetId);
        if (sessionId) break;
        await new Promise(r => setTimeout(r, 100));
      }
      if (!sessionId) {
        const attached = await send('Target.attachToTarget', { targetId, flatten: true });
        sessionId = attached.sessionId;
      }
      if (report.citadel_tab_scan) report.citadel_tab_scan.selected_target_id = targetId;
      report.tab_source = source;
    }

	    // Find existing Citadel tab or create one
	    const targets = await send('Target.getTargets', {});
	    const citadelTargets = prioritizedCitadelTargets(
	      (targets.targetInfos || []).filter(t => t.type === 'page' && t.url && t.url.includes('bkiconnect.com'))
	    );
	    report.citadel_tab_scan = {
	      attempted: !!(REUSE_TAB || directAuthIncompleteStatus),
	      candidate_count: citadelTargets.length,
	      scan_limit: TAB_SCAN_LIMIT,
	      scan_fetch_timeout_ms: TAB_SCAN_FETCH_TIMEOUT_MS,
	      scan_target_id_requested: !!TAB_SCAN_TARGET_ID,
	      scan_target_id_found: TAB_SCAN_TARGET_ID ? citadelTargets.some(t => t.targetId === TAB_SCAN_TARGET_ID) : null,
	      scanned_count: 0,
	      scan_skipped_count: 0,
	      authenticated_found: false,
	      status_count: 0,
	      status_overflow_count: 0,
	      statuses: [],
	      rescans: [],
	      api_headers_shape: {
	        accept: true,
	        correlation_id: true,
        mobile_source_id: !!currentMobileSourceId,
      },
    };
    function pushCitadelTabScanStatus(status) {
      report.citadel_tab_scan.status_count = (report.citadel_tab_scan.status_count || 0) + 1;
      if (report.citadel_tab_scan.statuses.length < 80) {
        report.citadel_tab_scan.statuses.push(status);
      } else {
        report.citadel_tab_scan.status_overflow_count = (report.citadel_tab_scan.status_overflow_count || 0) + 1;
      }
    }
    async function scanAuthenticatedCitadelTargets(stage) {
	      const currentTargets = stage === 'initial'
	        ? { targetInfos: targets.targetInfos || [] }
	        : await send('Target.getTargets', {});
	      const candidates = prioritizedCitadelTargets(
	        (currentTargets.targetInfos || []).filter(t => t.type === 'page' && t.url && t.url.includes('bkiconnect.com'))
	      );
	      if (TAB_SCAN_TARGET_ID) {
	        report.citadel_tab_scan.scan_target_id_found = candidates.some(t => t.targetId === TAB_SCAN_TARGET_ID);
	      }
	      const candidatesToScan = candidates.slice(0, TAB_SCAN_LIMIT);
	      const skippedCount = Math.max(0, candidates.length - candidatesToScan.length);
	      report.citadel_tab_scan.latest_candidate_count = candidates.length;
	      report.citadel_tab_scan.latest_scanned_count = candidatesToScan.length;
	      report.citadel_tab_scan.scan_skipped_count = (report.citadel_tab_scan.scan_skipped_count || 0) + skippedCount;
	      report.citadel_tab_scan.rescans.push({
	        stage,
	        candidate_count: candidates.length,
	        scanned_count: candidatesToScan.length,
	        skipped_count: skippedCount,
	        at_ms: Date.now() - authStartBase,
	      });
	      for (const candidate of candidatesToScan) {
	        report.citadel_tab_scan.scanned_count = (report.citadel_tab_scan.scanned_count || 0) + 1;
	        let scanSession = null;
	        let shouldDetach = false;
	        try {
          if (candidate.targetId === targetId && sessionId) {
            scanSession = sessionId;
          } else {
            const attached = await send('Target.attachToTarget', { targetId: candidate.targetId, flatten: true });
            scanSession = attached.sessionId;
            shouldDetach = true;
          }
          await send('Runtime.enable', {}, scanSession);
          const scan = await send('Runtime.evaluate', {
            awaitPromise: true,
            returnByValue: true,
            expression: `
	              (async () => {
	                try {
	                  const controller = new AbortController();
	                  const timer = setTimeout(() => controller.abort(), ${TAB_SCAN_FETCH_TIMEOUT_MS});
                  const options = ${apiFetchOptionsBrowserExpression(false, true)};
                  const authSnapshot = options.__citadelAuthSnapshot || {};
                  delete options.__citadelAuthSnapshot;
                  options.signal = controller.signal;
                  const resp = await fetch('${API_BASE}/sd/loan-info/v2/my/loans-summary', options);
                  clearTimeout(timer);
                  return {
                    ok: resp.ok,
                    status: resp.status,
                    state: location.hash || location.pathname,
                    auth: authSnapshot
                  };
                } catch (e) {
                  return { ok: false, error: e.message, state: location.hash || location.pathname };
                }
              })()
            `,
          }, scanSession).catch(error => ({ result: { value: { ok: false, error: String(error && error.message || error) } } }));
          const value = scan && scan.result && scan.result.value || {};
          pushCitadelTabScanStatus({
            stage,
            url: candidate.url,
            status: value.status || null,
            ok: !!value.ok,
            state: value.state || null,
            browser_bearer_token_available: !!(value.auth && value.auth.bearer_token_available),
            browser_mobile_source_id_available: !!(value.auth && value.auth.mobile_source_id_available),
            error: value.error ? String(value.error).slice(0, 120) : null,
          });
          if (value.ok) {
            targetId = candidate.targetId;
            sessionId = scanSession;
            report.citadel_tab_scan.authenticated_found = true;
            report.citadel_tab_scan.authenticated_stage = stage;
            report.citadel_tab_scan.selected_target_id = targetId;
            report.tab_source = stage === 'initial' ? 'existing_authenticated_scan' : 'existing_authenticated_rescan';
            return candidate;
          }
        } catch (e) {
          pushCitadelTabScanStatus({
            stage,
            url: candidate.url,
            status: null,
            ok: false,
            error: String(e && e.message || e).slice(0, 120),
          });
        }
        if (scanSession && shouldDetach) {
          await send('Target.detachFromTarget', { sessionId: scanSession }).catch(() => null);
        }
      }
      return null;
    }

    let existing = null;
    const authStartBase = Date.now();
	    if ((REUSE_TAB || directAuthIncompleteStatus) && (!NEW_TAB || TAB_SCAN_TARGET_ID || LOGIN_MODE === 'manual')) {
	      existing = await scanAuthenticatedCitadelTargets('initial');
	      if (!existing && REUSE_TAB && LOGIN_MODE === 'manual') {
	        const pinnedTarget = TAB_SCAN_TARGET_ID
	          ? citadelTargets.find(t => t.targetId === TAB_SCAN_TARGET_ID)
	          : null;
	        existing = pinnedTarget || citadelTargets[0] || null;
	        if (existing) {
	          report.citadel_tab_scan.manual_wait_target_selected = true;
	          report.citadel_tab_scan.manual_wait_target_id_requested = !!TAB_SCAN_TARGET_ID;
	          report.citadel_tab_scan.manual_wait_target_id_found = !!pinnedTarget || !TAB_SCAN_TARGET_ID;
	          report.tab_source = pinnedTarget ? 'manual_pinned_existing' : 'manual_existing';
	        }
	      }
	      if (!existing && REUSE_TAB && LOGIN_MODE !== 'manual') {
	        existing = citadelTargets[0] || null;
	      }
      if (!existing && directAuthIncompleteStatus && LOGIN_MODE === 'direct' && !directAuthReachedVerifiedOtpRequest) {
        const pinnedTarget = TAB_SCAN_TARGET_ID
          ? citadelTargets.find(t => t.targetId === TAB_SCAN_TARGET_ID)
          : null;
        existing = pinnedTarget || citadelTargets[0] || null;
        if (existing) {
          report.citadel_tab_scan.direct_fallback_target_selected = true;
          report.citadel_tab_scan.direct_fallback_target_id_requested = !!TAB_SCAN_TARGET_ID;
          report.citadel_tab_scan.direct_fallback_target_id_found = !!pinnedTarget || !TAB_SCAN_TARGET_ID;
          report.tab_source = pinnedTarget ? 'direct_fallback_pinned_existing' : 'direct_fallback_existing';
        }
      }
	    }
    if (directAuthIncompleteStatus && report.direct_auth) {
      report.direct_auth.browser_fallback_authenticated_found = !!(existing && report.citadel_tab_scan.authenticated_found);
    }
    if (!existing && directAuthIncompleteStatus && LOGIN_MODE === 'direct') {
      report.status = directAuthIncompleteStatus === 'otp_required' ? 'otp_required' : 'auth_failed';
      report.auth_failure_reason = directAuthIncompleteStatus;
      report.errors.push(`Citadel direct authentication did not complete: ${directAuthIncompleteStatus}`);
      if (report.status === 'auth_failed') {
        writeManualAuthRequired(report, directAuthIncompleteStatus || 'direct_auth_incomplete');
      }
      report.ended_at = new Date().toISOString();
      writeReport(report, { archiveLiveAuth: true });
      console.error(`[citadel] Direct auth incomplete: ${directAuthIncompleteStatus}`);
      if (authBrowser) await authBrowser.close();
      process.exit(report.status === 'otp_required' ? 3 : 1);
    }
    if (existing) {
      targetId = existing.targetId;
      if (!sessionId) {
        const attached = await send('Target.attachToTarget', { targetId, flatten: true });
        sessionId = attached.sessionId;
      }
		      if (!String(report.tab_source || '').startsWith('manual_') && !String(report.tab_source || '').startsWith('direct_fallback_')) {
		        report.tab_source = report.citadel_tab_scan.authenticated_found ? 'existing_authenticated_scan' : 'existing';
		      }
      report.citadel_tab_scan.selected_target_id = targetId;
    } else {
      const result = await send('Target.createTarget', { url: PORTAL_URL });
      targetId = result.targetId;
      await send('Target.activateTarget', { targetId }).catch(() => null);
      report.citadel_tab_scan.selected_target_id = targetId;
      for (let i = 0; i < 120; i++) {
        sessionId = sessions.get(targetId);
        if (sessionId) break;
        await new Promise(r => setTimeout(r, 100));
      }
      report.tab_source = 'created';
    }
    if (!sessionId) throw new Error('no CDP session attached');

    try {
      await enablePageSession(sessionId, LOGIN_MODE === 'manual');
    } catch (e) {
      if (report.tab_source !== 'existing') throw e;
      report.warnings.push(`Existing Citadel tab was not CDP-responsive; opened a fresh tab: ${e.message}`);
      await createFreshCitadelTarget('created_after_existing_cdp_timeout');
      await enablePageSession(sessionId, LOGIN_MODE === 'manual');
    }

    async function evalExpr(expression, awaitPromise = true) {
      const res = await send('Runtime.evaluate', { expression, awaitPromise, returnByValue: true }, sessionId);
      if (res.result && res.result.subtype === 'error') throw new Error(res.result.description || 'eval error');
      return res.result ? res.result.value : undefined;
    }

    async function collectBrowserAuthSnapshot(stage) {
      const raw = await evalExpr(`
        (() => {
          ${browserStorageAuthHelperSource()}
          const auth = __citadelStorageAuth();
          return JSON.stringify({
            stage: ${JSON.stringify(stage)},
            bearer_token_available: !!auth.bearerToken,
            bearer_token_length: auth.bearerToken ? String(auth.bearerToken).length : 0,
            bearer_token_source_available: !!auth.bearerTokenSource,
            mobile_source_id_available: !!auth.mobileSourceId,
            local_storage_key_count: auth.localStorageKeyCount,
            session_storage_key_count: auth.sessionStorageKeyCount,
            token_candidate_count: auth.tokenCandidateCount,
            url_path: location.pathname,
            hash_present: !!location.hash
          });
        })()
      `).catch(() => null);
      if (!raw) return null;
      let snapshot = null;
      try { snapshot = JSON.parse(raw); } catch { return null; }
      report.browser_auth_snapshots = report.browser_auth_snapshots || [];
      report.browser_auth_snapshots.push(snapshot);
      report.browser_storage_bearer_token_available = !!snapshot.bearer_token_available;
      report.browser_storage_mobile_source_id_available = !!snapshot.mobile_source_id_available;
      report.browser_storage_token_candidate_count = snapshot.token_candidate_count || 0;
      return snapshot;
    }

    async function loginPageState() {
      return await evalExpr(`
        (() => {
          const e = document.querySelector('input[name="username"],input#username,input[type="text"]');
          const p = document.querySelector('input[name="password"],input#password,input[type="password"]');
          const agree = document.querySelector('input#agreeToTerms-input,input[name="agree"]');
          const btn = document.querySelector('button[type="submit"],button#focusid8');
          const bodyText = (document.body && document.body.innerText || '').replace(/\\s+/g, ' ');
          let state = 'login';
          let visibleError = '';
          if (/verify your account/i.test(bodyText) || /one[- ]?time|verification code|enter code/i.test(bodyText)) state = 'mfa';
          const loginNotRecognized = bodyText.match(/Sorry, we don't recognize that login\\. Please review the information you provided\\./i);
          if (loginNotRecognized) {
            state = 'login_not_recognized';
            visibleError = loginNotRecognized[0];
          }
          return {
            url: location.href,
            title: document.title,
            readyState: document.readyState,
            hasUsername: !!e,
            hasPassword: !!p,
            hasAgree: !!agree,
            agreeChecked: !!(agree && agree.checked),
            hasButton: !!btn,
            buttonDisabled: !!(btn && btn.disabled),
            state,
            visibleError,
          };
        })()
      `).catch(() => null);
    }

    async function waitForLoginPageState(waitMs = 20000) {
      const started = Date.now();
      let lastState = null;
      while ((Date.now() - started) < waitMs) {
        lastState = await loginPageState();
        if (lastState && (lastState.hasUsername || lastState.hasPassword || lastState.state !== 'login')) return lastState;
        await new Promise(r => setTimeout(r, 500));
      }
      return lastState;
    }

    function apiFetchOptionsExpression() {
      report.api_headers_shape = {
        accept: true,
        correlation_id: true,
        mobile_source_id: !!currentMobileSourceId,
        authorization: !!directAuthToken || !!report.browser_storage_bearer_token_available,
      };
      return apiFetchOptionsBrowserExpression(true);
    }

    async function focusAndClear(selector) {
      return await evalExpr(`
        (() => {
          const el = document.querySelector(${JSON.stringify(selector)});
          if (!el) return false;
          el.focus();
          const desc = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
          desc.set.call(el, '');
          el.dispatchEvent(new Event('input', {bubbles:true}));
          el.dispatchEvent(new Event('change', {bubbles:true}));
          return true;
        })()
      `, false);
    }

    async function typeInto(selector, value) {
      const focused = await focusAndClear(selector);
      if (!focused) return false;
      await send('Input.insertText', { text: value }, sessionId);
      await evalExpr(`
        (() => {
          const el = document.querySelector(${JSON.stringify(selector)});
          if (!el) return false;
          el.dispatchEvent(new Event('input', {bubbles:true}));
          el.dispatchEvent(new Event('change', {bubbles:true}));
          el.blur();
          return true;
        })()
      `, false);
      return true;
    }

    async function clickSelector(selector) {
      const rect = await evalExpr(`
        (() => {
          const el = document.querySelector(${JSON.stringify(selector)});
          if (!el) return null;
          const rect = el.getBoundingClientRect();
          if (!rect || rect.width <= 0 || rect.height <= 0) return null;
          return {
            x: rect.left + rect.width / 2,
            y: rect.top + rect.height / 2,
            disabled: !!el.disabled,
          };
        })()
      `);
      if (!rect || rect.disabled) return false;
      await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: rect.x, y: rect.y }, sessionId);
      await send('Input.dispatchMouseEvent', { type: 'mousePressed', x: rect.x, y: rect.y, button: 'left', clickCount: 1 }, sessionId);
      await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: rect.x, y: rect.y, button: 'left', clickCount: 1 }, sessionId);
      return true;
    }

    async function submitLoginForm(creds) {
      const state = await waitForLoginPageState();
      report.login_page_state = state;
      if (!state || !state.hasUsername || !state.hasPassword) return 'no_form';
      const typedUsername = await typeInto('input[name="username"],input#username,input[type="text"]', creds.email);
      const typedPassword = await typeInto('input[name="password"],input#password,input[type="password"]', creds.password);
      if (!typedUsername || !typedPassword) return 'type_failed';
      await new Promise(r => setTimeout(r, 250));
      const submitted = await evalExpr(`
        (() => {
          const agree = document.querySelector('input#agreeToTerms-input,input[name="agree"]');
          if (agree && !agree.checked) {
            agree.click();
            agree.dispatchEvent(new Event('change', {bubbles:true}));
          }
          return true;
        })()
      `, false);
      await new Promise(r => setTimeout(r, 250));
      const clicked = await clickSelector('button[type="submit"],button#focusid8');
      const fallbackSubmitted = clicked ? 'submitted_trusted_click' : await evalExpr(`
        (() => {
          const btn = document.querySelector('button[type="submit"],button#focusid8');
          if (!btn || btn.disabled) return 'button_unavailable';
          btn.focus();
          btn.click();
          return 'submitted';
        })()
      `, false);
      await new Promise(r => setTimeout(r, 5000));
      report.login_page_state_after_submit = await loginPageState();
      return fallbackSubmitted || (submitted ? 'submitted' : 'submit_failed');
    }

    function oauthPasswordGrantFailureReason() {
      const events = Array.isArray(report.oauth_network_events) ? report.oauth_network_events : [];
      const passwordGrantFailures = events.filter(event => {
        const bodyKeys = event && event.request_shape && Array.isArray(event.request_shape.body_keys)
          ? event.request_shape.body_keys
          : [];
        return bodyKeys.includes('username') && bodyKeys.includes('password') && event.status >= 400;
      });
      report.oauth_password_grant_failure_count = passwordGrantFailures.length;
      const errorCodes = [...new Set(passwordGrantFailures.flatMap(event => {
        const summary = event && event.error_summary || {};
        return Array.isArray(summary.error_message_codes) ? summary.error_message_codes : [];
      }).map(String))].sort();
      report.oauth_password_grant_error_codes = errorCodes;
      if (errorCodes.includes('20000')) return 'credential_rejected_before_mfa';
      return '';
    }

    async function captureFailureArtifacts() {
      try {
        const html = await evalExpr('document.documentElement ? document.documentElement.outerHTML : ""', false);
        if (html) fs.writeFileSync(htmlDumpPath, html, 'utf8');
      } catch {}
      try {
        const shot = await send('Page.captureScreenshot', { format: 'png' }, sessionId);
        if (shot && shot.data) fs.writeFileSync(screenshotPath, Buffer.from(shot.data, 'base64'));
      } catch {}
    }

    // Wait for authentication: check if we can access the loans-summary API
    report.auth_wait_started = true;
    await collectBrowserAuthSnapshot('selected_tab_initial');
    let authed = false;
    let loginSubmitted = false;
    const authStart = Date.now();
    while ((Date.now() - authStart) < AUTH_WAIT_MS) {
      const checkResult = await evalExpr(`
        (async () => {
          try {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 8000);
            const options = ${apiFetchOptionsExpression()};
            options.signal = controller.signal;
            const resp = await fetch('${API_BASE}/sd/loan-info/v2/my/loans-summary', options);
            clearTimeout(timer);
            if (resp.ok) return { ok: true, status: resp.status };
            return { ok: false, status: resp.status };
          } catch(e) {
            return { ok: false, error: e.message };
          }
        })()
      `).catch(() => ({ ok: false, error: 'eval failed' }));

      if (checkResult && checkResult.ok) {
        authed = true;
        break;
      }
      if (LOGIN_MODE === 'manual' && REUSE_TAB) {
        const authenticatedTab = await scanAuthenticatedCitadelTargets('auth_wait');
        if (authenticatedTab) {
          await send('Target.activateTarget', { targetId }).catch(() => null);
          await enablePageSession(sessionId, true);
          authed = true;
          break;
        }
      }
      // Auto/direct modes submit credentials through the visible CDP browser.
      // This never launches a headless browser.
      if ((LOGIN_MODE === 'auto' || (LOGIN_MODE === 'direct' && directAuthIncompleteStatus)) && creds && !loginSubmitted) {
        try {
          const loginResult = await submitLoginForm(creds);
          report.login_form_last_result = loginResult;
          if (String(loginResult || '').startsWith('submitted')) {
            loginSubmitted = true;
            report.login_form_submitted = true;
          }
        } catch {}
      }
      await new Promise(r => setTimeout(r, 2000));
    }

    if (!authed) {
      report.login_page_state_final = await loginPageState();
      if (report.login_page_state_final && report.login_page_state_final.visibleError) {
        report.auth_visible_error = report.login_page_state_final.visibleError;
      }
      const oauthFailureReason = oauthPasswordGrantFailureReason();
      if (oauthFailureReason) {
        report.status = 'auth_failed';
        report.auth_failure_reason = oauthFailureReason;
        if (report.login_page_state_final && report.login_page_state_final.state === 'login_not_recognized') {
          report.auth_failure_visible_reason = 'login_not_recognized';
        }
        applyCredentialStateDriftAssessment(report, report, oauthFailureReason === 'credential_rejected_before_mfa');
        report.errors.push('Citadel rejected the submitted login before MFA');
      } else if (report.login_page_state_final && report.login_page_state_final.state === 'login_not_recognized') {
        report.status = 'auth_failed';
        report.auth_failure_reason = 'login_not_recognized';
        report.auth_failure_visible_reason = 'login_not_recognized';
        applyCredentialStateDriftAssessment(report, report, true);
        report.errors.push('Citadel rejected the submitted login');
      } else {
        report.status = 'auth_timeout';
        report.errors.push('Timed out waiting for Citadel authentication');
      }
      writeManualAuthRequired(report, report.auth_failure_visible_reason || report.auth_failure_reason || report.status);
      await captureFailureArtifacts();
      report.ended_at = new Date().toISOString();
      writeReport(report, { archiveLiveAuth: true });
      console.error(`[citadel] ${report.status}${report.auth_failure_reason ? `: ${report.auth_failure_reason}` : ''}`);
      process.exit(1);
    }
    report.authed = true;

    // Fetch eStatements list
    const estmtResult = await evalExpr(`
      (async () => {
        try {
          const resp = await fetch('${API_BASE}/sd/loan-maint/v2/my/loans/${LOAN_ORG}/${LOAN_ID}/eStatements', ${apiFetchOptionsExpression()});
          const text = await resp.text();
          return { status: resp.status, text };
        } catch(e) {
          return { status: 0, error: e.message };
        }
      })()
    `);
    report.api_responses.eStatements = { status: estmtResult.status, textLen: (estmtResult.text || '').length };
    let eStatements = parseJsonList(
      estmtResult.text,
      ['statements', 'Statements', 'eStatements', 'EStatements', 'data', 'Data'],
      'eStatements',
      report,
    );
    report.eStatements_count = Array.isArray(eStatements) ? eStatements.length : 0;

    // Fetch docs list (full document repository)
    const docsResult = await evalExpr(`
      (async () => {
        try {
          const resp = await fetch('${API_BASE}/lsd/api/v1/my/loans/${LOAN_ORG}/${LOAN_ID}/docs', ${apiFetchOptionsExpression()});
          const text = await resp.text();
          return { status: resp.status, text };
        } catch(e) {
          return { status: 0, error: e.message };
        }
      })()
    `);
    report.api_responses.docs = { status: docsResult.status, textLen: (docsResult.text || '').length };
    let docs = parseJsonList(
      docsResult.text,
      ['Documents', 'documents', 'docs', 'Docs', 'data', 'Data'],
      'docs',
      report,
    );
    report.docs_count = Array.isArray(docs) ? docs.length : 0;

    // Find statement documents (mortgage/billing statements)
    const statementDocs = (Array.isArray(docs) ? docs : []).filter(d => {
      const name = String(coalesce(
        d.name,
        d.Name,
        d.documentName,
        d.DocumentName,
        d.title,
        d.Title,
        d.type,
        d.Type,
        d.DocumentType,
        d.Description,
      ) || '').toLowerCase();
      return name.includes('statement') || name.includes('billing') || name.includes('mortgage');
    });
    report.statement_doc_count = statementDocs.length;

    // Combine candidates from eStatements and docs
    const candidates = [];
    for (const s of eStatements) {
      candidates.push({ source: 'eStatements', doc: s });
    }
    for (const d of statementDocs) {
      candidates.push({ source: 'docs', doc: d });
    }

    // Download each statement
    for (const candidate of candidates) {
      const doc = candidate.doc;
      const docId = docField(doc, 'id', 'Id', 'ID', 'documentId', 'DocumentId', 'DocumentID', 'docId', 'DocId', 'DocumentIdentifier');
      let docName = docField(doc, 'name', 'Name', 'documentName', 'DocumentName', 'title', 'Title', 'type', 'Type', 'DocumentType') || '';
      let docDate = docField(doc, 'date', 'Date', 'statementDate', 'StatementDate', 'createdDate', 'CreatedDate', 'postedDate', 'PostedDate', 'DocumentDate', 'documentDate', 'DocumentCreationDate', 'documentCreationDate', 'creationDate', 'CreationDate') || '';
      let docMime = docField(doc, 'mimeType', 'MimeType', 'contentType', 'ContentType', 'DocumentMimeType') || '';

      // Try to find or construct a download URL
      let downloadUrl = docField(doc, 'url', 'Url', 'URL', 'downloadUrl', 'DownloadUrl', 'DownloadURL', 'link', 'Link', 'href', 'Href');
      let pdfBase64 = pdfBase64FromJson(doc);

      // If no direct URL, try the individual doc endpoint
      if (!downloadUrl && !pdfBase64 && docId) {
        const docDetail = await evalExpr(`
          (async () => {
            try {
              const resp = await fetch('${API_BASE}/lsd/api/v1/my/loans/${LOAN_ORG}/${LOAN_ID}/docs/${docId}', ${apiFetchOptionsExpression()});
              const text = await resp.text();
              return { status: resp.status, text };
            } catch(e) {
              return { status: 0, error: e.message };
            }
          })()
        `).catch(() => ({ status: 0, text: '' }));

        try {
          const detail = JSON.parse(docDetail.text || '{}');
          downloadUrl = docField(detail, 'url', 'Url', 'URL', 'downloadUrl', 'DownloadUrl', 'DownloadURL', 'link', 'Link', 'href', 'Href');
          pdfBase64 = pdfBase64FromJson(detail);
          docName = docName || docField(detail, 'name', 'Name', 'documentName', 'DocumentName', 'title', 'Title') || '';
          docDate = docDate || docField(detail, 'date', 'Date', 'statementDate', 'StatementDate', 'createdDate', 'CreatedDate', 'postedDate', 'PostedDate', 'DocumentDate', 'documentDate', 'DocumentCreationDate', 'documentCreationDate', 'creationDate', 'CreationDate') || '';
          docMime = docMime || docField(detail, 'mimeType', 'MimeType', 'contentType', 'ContentType', 'DocumentMimeType') || '';
        } catch {}
      }

      // Determine the statement date for filename
      let dateStr = statementDateFromDoc(docName, docDate);

      if (!dateStr) {
        report.skipped_files.push({ name: docName, id: docId, reason: 'no_date_found', source: candidate.source });
        continue;
      }

      const filePath = statementPathForDate(dateStr);
      if (!filePath) {
        report.skipped_files.push({ name: docName, id: docId, reason: 'invalid_date', source: candidate.source });
        continue;
      }

      const fileName = path.basename(filePath);
      const statementMonth = dateStr.slice(0, 7);
      if (existingNames.has(fileName)) {
        report.skipped_files.push({ name: fileName, id: docId, reason: 'already_exists', source: candidate.source, statement_date: dateStr, statement_month: statementMonth });
        continue;
      }

      // Download the PDF
      try {
        let pdfBuffer = null;
        if (pdfBase64) {
          pdfBuffer = pdfBufferFromBase64(pdfBase64);
        } else if (downloadUrl) {
          const fullUrl = downloadUrl.startsWith('http') ? downloadUrl : `${API_BASE}${downloadUrl}`;
          const dlResult = await evalExpr(`
            (async () => {
              try {
                const resp = await fetch('${fullUrl}', ${apiFetchOptionsExpression()});
                const buf = await resp.arrayBuffer();
                const bytes = new Uint8Array(buf);
                let binary = '';
                for (let i = 0; i < bytes.length; i++) {
                  binary += String.fromCharCode(bytes[i]);
                }
                return btoa(binary);
              } catch(e) {
                return null;
              }
            })()
          `);
          if (dlResult) {
            pdfBuffer = Buffer.from(dlResult, 'base64');
          }
        }

        if (pdfBuffer && pdfBuffer.length > 1000) {
          fs.writeFileSync(filePath, pdfBuffer);
          report.downloaded_files.push({ name: fileName, id: docId, size: pdfBuffer.length, source: candidate.source, statement_date: dateStr, statement_month: statementMonth });
          existingNames.add(fileName);
        } else {
          report.errors.push(`Download too small or empty for ${fileName} (docId=${docId})`);
        }
      } catch (e) {
        report.errors.push(`Download failed for ${fileName}: ${e.message}`);
      }
    }

    refreshTargetMonthSummary(report);
    report.status = report.errors.length > 0 ? 'partial' : 'ok';
    report.ended_at = new Date().toISOString();
    writeReport(report, { archiveLiveAuth: true });
    console.log(`[citadel] Downloaded ${report.downloaded_files.length}, skipped ${report.skipped_files.length}, errors ${report.errors.length}`);
    if (authBrowser) await authBrowser.close();
    process.exit(0);
  } catch (err) {
    report.status = 'error';
    report.errors.push(err.message);
    report.ended_at = new Date().toISOString();
    writeReport(report, { archiveLiveAuth: true });
    console.error(`[citadel] ERROR: ${err.message}`);
    if (authBrowser) await authBrowser.close();
    process.exit(1);
  }
}

main().catch(err => {
  console.error(String(err && err.message || err));
  process.exit(1);
});
