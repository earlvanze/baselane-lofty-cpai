#!/usr/bin/env node
const fs = require('fs');
const path = require('path');
const { randomUUID } = require('crypto');

const OPENCLAW_ROOT = process.env.OPENCLAW_ROOT || path.join(process.env.HOME || '', '.openclaw');
const ROOT = process.env.WORKSPACE_ROOT || path.join(OPENCLAW_ROOT, 'workspace');
const REPORTS = path.join(ROOT, 'reports');
const VERSION_URL = process.env.CITADEL_CDP_VERSION_URL || process.env.BASELANE_CDP_VERSION_URL || 'http://127.0.0.1:9222/json/version';
const API_BASE = process.env.CITADEL_API_BASE || 'https://api.bkiconnect.com';
const PORTAL_URL = 'https://loansphereservicingdigital.bkiconnect.com/mycitadelservicing/#/login';
const SOURCE_HAR_PATH = process.env.CITADEL_CAPTURE_SOURCE_HAR_PATH
  || process.env.CITADEL_HAR_PATH
  || '/mnt/f/har/citadel_loansphereservicingdigital.bkiconnect.com.har';
const LOAN_ORG = process.env.CITADEL_LOAN_ORG || '414';
const LOAN_ID = process.env.CITADEL_LOAN_ID || '1910008671';
const AUTH_WAIT_MS = positiveInt(process.env.CITADEL_AUTH_WAIT_MS, 300000);
const SCAN_LIMIT = positiveInt(process.env.CITADEL_TAB_SCAN_LIMIT, 8);
const FETCH_TIMEOUT_MS = positiveInt(process.env.CITADEL_TAB_SCAN_FETCH_TIMEOUT_MS, 3000);
const CDP_COMMAND_TIMEOUT_MS = positiveInt(process.env.CITADEL_CDP_COMMAND_TIMEOUT_MS, 15000);
const DOC_LIMIT = positiveInt(process.env.CITADEL_CAPTURE_DOC_LIMIT, 20);
const SOURCE_DIRECT_PDF_LIMIT = positiveInt(process.env.CITADEL_CAPTURE_SOURCE_DIRECT_PDF_LIMIT, 10);
const TARGET_ID = (process.env.CITADEL_TAB_SCAN_TARGET_ID || '').trim();
const CAPTURE_HAR_PATH = process.env.CITADEL_CAPTURE_HAR_PATH || path.join(REPORTS, 'citadel_replay_capture.har');
const REPORT_PATH = process.env.CITADEL_CAPTURE_REPORT || path.join(REPORTS, 'citadel_replay_capture_report.json');
const MANUAL_AUTH_FILE = process.env.CITADEL_CAPTURE_MANUAL_AUTH_FILE || path.join(REPORTS, 'citadel_manual_capture_required.json');
const RESPONSE_BODY_REQUIREMENTS = parseResponseBodyRequirements();
const REQUIRED_RESPONSE_PATHS = parseRequiredResponsePaths();
const REQUIRED_RESPONSE_PATH_COUNTS = parseRequiredResponsePathCounts();
const MANUAL_CAPTURE_COMMAND = 'scripts/citadel_manual_capture_then_download.sh';
const CAPTURE_ONLY_COMMAND = 'scripts/citadel_manual_capture_har.sh';
const VOLATILE_REPORT_FIELDS = new Set(['started_at', 'ended_at', 'written_at']);
const VOLATILE_HAR_FIELDS = new Set(['startedDateTime']);

function withoutVolatileFields(value, volatileFields) {
  if (Array.isArray(value)) return value.map(item => withoutVolatileFields(item, volatileFields));
  if (!value || typeof value !== 'object') return value;
  return Object.fromEntries(
    Object.entries(value)
      .filter(([key]) => !volatileFields.has(key))
      .map(([key, item]) => [key, withoutVolatileFields(item, volatileFields)]),
  );
}

function restoreVolatileFields(current, previous, volatileFields) {
  if (Array.isArray(current) && Array.isArray(previous) && current.length === previous.length) {
    return current.map((item, index) => restoreVolatileFields(item, previous[index], volatileFields));
  }
  if (!current || !previous || typeof current !== 'object' || typeof previous !== 'object' || Array.isArray(current) || Array.isArray(previous)) {
    return current;
  }
  const restored = { ...current };
  for (const [key, value] of Object.entries(current)) {
    if (volatileFields.has(key) && Object.prototype.hasOwnProperty.call(previous, key)) {
      restored[key] = previous[key];
    } else if (Object.prototype.hasOwnProperty.call(previous, key)) {
      restored[key] = restoreVolatileFields(value, previous[key], volatileFields);
    }
  }
  return restored;
}

function preserveVolatileFieldsIfUnchanged(report, reportPath, volatileFields) {
  let previous = null;
  try {
    previous = JSON.parse(fs.readFileSync(reportPath, 'utf8'));
  } catch {
    return report;
  }
  const currentStable = JSON.stringify(withoutVolatileFields(report, volatileFields));
  const previousStable = JSON.stringify(withoutVolatileFields(previous, volatileFields));
  if (currentStable !== previousStable) return report;
  return restoreVolatileFields(report, previous, volatileFields);
}

function writeStableJson(reportPath, report, volatileFields = VOLATILE_REPORT_FIELDS) {
  const stableReport = preserveVolatileFieldsIfUnchanged(report, reportPath, volatileFields);
  const content = `${JSON.stringify(stableReport, null, 2)}\n`;
  let existing = null;
  try {
    existing = fs.readFileSync(reportPath, 'utf8');
  } catch {}
  if (existing !== content) {
    fs.mkdirSync(path.dirname(reportPath), { recursive: true });
    const tempPath = `${reportPath}.tmp-${process.pid}-${randomUUID()}`;
    fs.writeFileSync(tempPath, content, { mode: 0o600 });
    fs.renameSync(tempPath, reportPath);
  }
  return stableReport;
}

function captureHarInstallDecision(report) {
  if (!report || typeof report !== 'object') {
    return { ok: false, reason: 'capture_report_missing' };
  }
  if (report.status !== 'ok') {
    return { ok: false, reason: report.reason || report.status || 'capture_not_ready' };
  }
  if (report.target_month_replayable_document_available !== true) {
    return { ok: false, reason: 'target_month_replayable_document_unavailable' };
  }
  if (Number(report.target_month_replayable_document_payload_count || 0) <= 0) {
    return { ok: false, reason: 'target_month_replayable_document_payload_missing' };
  }
  if (Number(report.replayable_document_payload_count || 0) <= 0) {
    return { ok: false, reason: 'replayable_document_payload_missing' };
  }
  if (Array.isArray(report.missing_required_response_paths) && report.missing_required_response_paths.length) {
    return { ok: false, reason: 'missing_required_response_paths' };
  }
  if (Number(report.missing_response_body_requirement_count || 0) > 0) {
    return { ok: false, reason: 'missing_response_body_requirements' };
  }
  if (Number(report.captured_replayable_response_body_count || 0) <= 0) {
    return { ok: false, reason: 'captured_replayable_response_body_missing' };
  }
  return { ok: true, reason: null };
}

function shellQuote(value) {
  const text = String(value ?? '');
  if (text === '') return "''";
  return `'${text.replace(/'/g, `'\\''`)}'`;
}

function runDownloaderCommand(harPath = CAPTURE_HAR_PATH) {
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

function isPdfMime(value) {
  return String(value || '').toLowerCase().split(';')[0].trim() === 'application/pdf';
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

function currentTargetMonth() {
  const configured = String(
    process.env.CITADEL_TARGET_MONTH
    || process.env.MORTGAGE_STATEMENT_TARGET_MONTH
    || process.env.BASELANE_MORTGAGE_STATEMENT_TARGET_MONTH
    || process.env.BASELANE_MONTHLY_TARGET_STAMP
    || ''
  ).trim();
  if (/^20\d{2}-\d{2}$/.test(configured)) return configured;
  return new Date().toISOString().slice(0, 7);
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

function addUnique(values, value) {
  if (value && !values.includes(value)) values.push(value);
}

function jsonFromResult(result) {
  try {
    return JSON.parse(String(result && result.text || ''));
  } catch {
    return null;
  }
}

function targetPriority(target) {
  const url = String((target || {}).url || '');
  const title = String((target || {}).title || '');
  let score = 0;
  if (TARGET_ID && (target || {}).targetId === TARGET_ID) score += 1000;
  if (url.includes('loansphereservicingdigital.bkiconnect.com/mycitadelservicing')) score += 200;
  if (url.includes('mycitadelservicing') || title.toLowerCase().includes('citadel')) score += 100;
  if (!url.includes('#/login')) score += 50;
  if (/#\/(loan|loans|dashboard|home|account|documents|estatements|statements)/i.test(url)) score += 50;
  return score;
}

function prioritize(targets) {
  return [...targets].sort((a, b) => targetPriority(b) - targetPriority(a));
}

function targetRoute(target) {
  const url = String((target || {}).url || '');
  if (!url.includes('#')) return url;
  return url.split('#').slice(1).join('#') || '#';
}

function routeCounts(targets) {
  return targets.reduce((counts, target) => {
    const route = targetRoute(target);
    counts[route] = (counts[route] || 0) + 1;
    return counts;
  }, {});
}

function storageAuthHelperSource() {
  return `
    function __citadelStorageAuth() {
      const result = { bearerToken: '', mobileSourceId: '', tokenCandidateCount: 0 };
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
          if (!result.bearerToken) result.bearerToken = text.replace(/^Bearer\\s+/i, '');
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
        for (const [key, child] of Object.entries(value)) walk(child, source + '.' + key, depth + 1);
      }
      for (let i = 0; i < localStorage.length; i++) {
        const key = localStorage.key(i);
        walk(localStorage.getItem(key), 'localStorage.' + key, 0);
      }
      for (let i = 0; i < sessionStorage.length; i++) {
        const key = sessionStorage.key(i);
        walk(sessionStorage.getItem(key), 'sessionStorage.' + key, 0);
      }
      for (const key of Object.keys(window)) {
        if (/token|auth|bearer|source/i.test(key)) walk(window[key], 'window.' + key, 0);
      }
      return result;
    }
  `;
}

function parseList(text, keys) {
  try {
    return unwrapArray(JSON.parse(String(text || '')), keys);
  } catch {
    return [];
  }
}

function safeUrlPathWithQueryKeys(url) {
  try {
    const parsed = new URL(url);
    const keys = Array.from(parsed.searchParams.keys()).filter(Boolean).sort();
    return keys.length ? `${parsed.pathname}?${keys.join('&')}` : parsed.pathname;
  } catch {
    return String(url || '');
  }
}

function parseResponseBodyRequirements() {
  const rawJson = process.env.CITADEL_RESPONSE_BODY_REQUIREMENTS_JSON
    || process.env.CITADEL_REQUIRED_RESPONSE_BODY_REQUIREMENTS_JSON
    || '';
  if (!rawJson.trim()) return [];
  try {
    const parsed = JSON.parse(rawJson);
    if (!Array.isArray(parsed)) return [];
    return parsed.flatMap(item => {
      if (!item || typeof item !== 'object') return [];
      const requirementPath = String(item.path || '').trim();
      if (!requirementPath) return [];
      const missingCount = Number(item.missing_response_body_count);
      const directPdfMissingCount = Number(item.direct_pdf_missing_response_count);
      return [{
        path: requirementPath,
        role: String(item.role || 'workflow_response'),
        missing_response_body_count: Number.isFinite(missingCount) && missingCount > 0 ? Math.trunc(missingCount) : 1,
        direct_pdf_missing_response_count: Number.isFinite(directPdfMissingCount) && directPdfMissingCount > 0 ? Math.trunc(directPdfMissingCount) : 0,
        required_capture_quality: String(item.required_capture_quality || 'full_response_body'),
      }];
    });
  } catch {
    return [];
  }
}

function parseRequiredResponsePaths() {
  const rawJson = process.env.CITADEL_REQUIRED_RESPONSE_PATHS_JSON || '';
  if (rawJson.trim()) {
    try {
      const parsed = JSON.parse(rawJson);
      if (Array.isArray(parsed)) {
        const paths = parsed.filter(item => typeof item === 'string' && item.trim()).map(item => item.trim());
        if (paths.length) return [...new Set(paths)];
      }
    } catch {}
  }
  const requirementPaths = RESPONSE_BODY_REQUIREMENTS.map(item => item.path).filter(Boolean);
  if (requirementPaths.length) return [...new Set(requirementPaths)];
  const rawList = process.env.CITADEL_REQUIRED_RESPONSE_PATHS || '';
  if (rawList.trim()) {
    const paths = rawList
      .split(/\r?\n|,/)
      .map(item => item.trim())
      .filter(Boolean);
    if (paths.length) return [...new Set(paths)];
  }
  return defaultRequiredResponsePaths();
}

function parseRequiredResponsePathCounts() {
  const rawJson = process.env.CITADEL_REQUIRED_RESPONSE_PATH_COUNTS_JSON || '';
  const counts = {};
  if (rawJson.trim()) {
    try {
      const parsed = JSON.parse(rawJson);
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        for (const [key, value] of Object.entries(parsed)) {
          const pathKey = String(key || '').trim();
          const count = Number(value);
          if (pathKey && Number.isFinite(count) && count > 0) counts[pathKey] = Math.trunc(count);
        }
      }
    } catch {}
  }
  for (const requirement of RESPONSE_BODY_REQUIREMENTS) {
    const requiredPath = requirement.path;
    if (!requiredPath) continue;
    const requirementCount = Number(requirement.missing_response_body_count);
    if (Number.isFinite(requirementCount) && requirementCount > 0) {
      counts[requiredPath] = Math.max(counts[requiredPath] || 0, Math.trunc(requirementCount));
    }
  }
  for (const requiredPath of REQUIRED_RESPONSE_PATHS) {
    counts[requiredPath] = Math.max(1, counts[requiredPath] || 0);
  }
  return counts;
}

function replayablePayloadCount(text) {
  try {
    const json = JSON.parse(String(text || ''));
    return normalizeBase64Pdf(docField(json, 'content', 'Content', 'base64', 'Base64', 'data', 'Data', 'DocumentContent')) ? 1 : 0;
  } catch {
    return 0;
  }
}

function base64LooksLikePdf(text) {
  const normalized = normalizeBase64Pdf(text);
  if (!normalized) return false;
  try {
    const head = Buffer.from(String(normalized).slice(0, 96), 'base64').toString('latin1');
    return head.startsWith('%PDF');
  } catch {
    return false;
  }
}

function replayablePayloadCountFromResult(result) {
  if (
    isPdfMime(result && result.content_type)
    && result.content_encoding === 'base64'
    && base64LooksLikePdf(result && result.text)
  ) return 1;
  return replayablePayloadCount(result && result.text);
}

function harEntry(url, result) {
  const contentType = result.content_type || 'application/json';
  const text = result.text || '';
  const bodySize = Number.isFinite(Number(result.body_size)) ? Number(result.body_size) : String(text).length;
  const content = {
    size: bodySize,
    mimeType: contentType,
    text,
  };
  if (result.content_encoding) content.encoding = result.content_encoding;
  return {
    startedDateTime: new Date().toISOString(),
    time: 0,
    request: {
      method: 'GET',
      url,
      httpVersion: 'HTTP/2',
      headers: [],
      queryString: [],
      cookies: [],
      headersSize: -1,
      bodySize: 0,
    },
    response: {
      status: result.status || 0,
      statusText: result.status_text || '',
      httpVersion: 'HTTP/2',
      headers: contentType ? [{ name: 'content-type', value: contentType }] : [],
      cookies: [],
      content,
      redirectURL: '',
      headersSize: -1,
      bodySize,
    },
    cache: {},
    timings: { send: 0, wait: 0, receive: 0 },
  };
}

function envAssignment(name, value) {
  return `${name}=${shellQuote(value)}`;
}

function manualAuthCaptureEnv(targetId, { applyInstall = false } = {}) {
  const env = [];
  if (targetId) env.push(envAssignment('CITADEL_TAB_SCAN_TARGET_ID', targetId));
  env.push(envAssignment('CITADEL_CAPTURE_HAR_PATH', CAPTURE_HAR_PATH));
  env.push(envAssignment('CITADEL_CAPTURE_REPORT', REPORT_PATH));
  env.push('CITADEL_CAPTURE_INSTALL_VERIFIED_HAR=1');
  if (applyInstall) env.push('CITADEL_CAPTURE_INSTALL_VERIFIED_HAR_APPLY=1');
  env.push('CITADEL_CAPTURE_INSTALL_VERIFIED_HAR_ONLY=1');
  return env;
}

function manualAuthInstallDryRunCommand(targetId) {
  return [
    ...manualAuthCaptureEnv(targetId, { applyInstall: false }),
    MANUAL_CAPTURE_COMMAND,
  ].join(' ');
}

function manualAuthCommand(targetId) {
  return [
    ...manualAuthCaptureEnv(targetId, { applyInstall: true }),
    MANUAL_CAPTURE_COMMAND,
  ].join(' ');
}

function defaultRequiredResponsePaths() {
  return [
    `/sd/loan-maint/v2/my/loans/${LOAN_ORG}/${LOAN_ID}/eStatements`,
    `/lsd/api/v1/my/loans/${LOAN_ORG}/${LOAN_ID}/docs`,
    `/lsd/api/v1/my/loans/${LOAN_ORG}/${LOAN_ID}/docs/{documentId}`,
  ];
}

function requiredResponsePaths() {
  return REQUIRED_RESPONSE_PATHS;
}

function requiredResponsePathCounts() {
  return REQUIRED_RESPONSE_PATH_COUNTS;
}

function responseBodyRequirements() {
  return RESPONSE_BODY_REQUIREMENTS;
}

function pathFromEntry(entry) {
  return safeUrlPathWithQueryKeys(entry && entry.request && entry.request.url);
}

function entryHasResponseBody(entry) {
  return !!(entry && entry.response && entry.response.content && entry.response.content.text);
}

function entryResponseStatus(entry) {
  return Number(entry && entry.response && entry.response.status || 0);
}

function entryResponseContent(entry) {
  return entry && entry.response && entry.response.content || {};
}

function entryResponseText(entry) {
  const text = entryResponseContent(entry).text;
  return typeof text === 'string' ? text : '';
}

function entryHasSuccessfulStatus(entry) {
  const status = entryResponseStatus(entry);
  return status >= 200 && status < 300;
}

function mimeBase(value) {
  return String(value || '').toLowerCase().split(';')[0].trim();
}

function isJsonMime(value) {
  const mime = mimeBase(value);
  return mime === 'application/json' || mime.endsWith('+json');
}

function textLooksJson(text) {
  try {
    JSON.parse(String(text || ''));
    return true;
  } catch {
    return false;
  }
}

function textLooksHtml(text) {
  const value = String(text || '');
  return /^\s*</.test(value) || /<!doctype\s+html|<html[\s>]/i.test(value);
}

function entryHasReplayablePdfBody(entry) {
  if (!entryHasSuccessfulStatus(entry)) return false;
  const content = entryResponseContent(entry);
  return isPdfMime(content.mimeType)
    && content.encoding === 'base64'
    && base64LooksLikePdf(content.text);
}

function entryHasReplayableJsonBody(entry) {
  if (!entryHasSuccessfulStatus(entry)) return false;
  const content = entryResponseContent(entry);
  const text = entryResponseText(entry);
  if (!text || textLooksHtml(text)) return false;
  return isJsonMime(content.mimeType) || textLooksJson(text);
}

function entryHasReplayableGenericBody(entry) {
  if (!entryHasSuccessfulStatus(entry)) return false;
  const text = entryResponseText(entry);
  return !!text && !textLooksHtml(text);
}

function requirementNeedsPdfBody(requirement) {
  if (!requirement) return false;
  const role = String(requirement.role || '');
  const directPdfMissingCount = Number(requirement.direct_pdf_missing_response_count || 0);
  return role === 'direct_pdf' || directPdfMissingCount > 0;
}

function requirementNeedsJsonBody(requirement) {
  if (!requirement) return false;
  return ['document_list', 'document_detail', 'statement_index', 'workflow_response'].includes(String(requirement.role || ''));
}

function entrySatisfiesResponseBodyRequirement(entry, requirement = null) {
  if (requirementNeedsPdfBody(requirement)) return entryHasReplayablePdfBody(entry);
  if (requirementNeedsJsonBody(requirement)) return entryHasReplayableJsonBody(entry);
  return entryHasReplayableGenericBody(entry);
}

function requirementsForPath(requiredPath) {
  return responseBodyRequirements().filter(requirement => safePathMatchesRequiredPath(requiredPath, requirement.path));
}

function requiredPathSatisfied(requiredPath, entries) {
  return capturedRequiredResponsePathCount(requiredPath, entries) >= (requiredResponsePathCounts()[requiredPath] || 1);
}

function entryMatchesRequiredPath(entry, requiredPath) {
  const entryPath = pathFromEntry(entry);
  return safePathMatchesRequiredPath(entryPath, requiredPath);
}

function safePathMatchesRequiredPath(safePath, requiredPath) {
  if (!safePath || !requiredPath) return false;
  if (requiredPath.includes('{documentId}')) {
    const prefix = requiredPath.split('{documentId}')[0];
    return safePath.startsWith(prefix);
  }
  return safePath === requiredPath;
}

function capturedRequiredResponsePathCount(requiredPath, entries) {
  const requirements = requirementsForPath(requiredPath);
  return entries.filter(entry => {
    if (!entryMatchesRequiredPath(entry, requiredPath)) return false;
    if (!requirements.length) return entrySatisfiesResponseBodyRequirement(entry);
    return requirements.some(requirement => entrySatisfiesResponseBodyRequirement(entry, requirement));
  }).length;
}

function capturedRequiredResponsePaths(entries) {
  return requiredResponsePaths().filter(requiredPath => requiredPathSatisfied(requiredPath, entries));
}

function capturedRequiredResponsePathCounts(entries) {
  return Object.fromEntries(
    Object.keys(requiredResponsePathCounts()).map(requiredPath => [
      requiredPath,
      capturedRequiredResponsePathCount(requiredPath, entries),
    ]),
  );
}

function requiredResponsePathProgress(capturedCounts = {}, sourceCounts = {}) {
  const requiredCounts = requiredResponsePathCounts();
  return requiredResponsePaths().map(requiredPath => {
    const requiredCount = requiredCounts[requiredPath] || 1;
    const capturedCount = capturedCounts[requiredPath] || 0;
    const missingCount = Math.max(0, requiredCount - capturedCount);
    return {
      path: requiredPath,
      required_count: requiredCount,
      captured_count: capturedCount,
      missing_count: missingCount,
      satisfied: missingCount === 0,
      source_candidate_count: sourceCounts[requiredPath] || 0,
    };
  });
}

function missingRequiredResponsePathCounts(capturedCounts) {
  const missing = {};
  for (const [requiredPath, requiredCount] of Object.entries(requiredResponsePathCounts())) {
    const missingCount = requiredCount - (capturedCounts[requiredPath] || 0);
    if (missingCount > 0) missing[requiredPath] = missingCount;
  }
  return missing;
}

function requirementRoleCounts(requirements) {
  return requirements.reduce((counts, requirement) => {
    const role = requirement && requirement.role || 'workflow_response';
    counts[role] = (counts[role] || 0) + 1;
    return counts;
  }, {});
}

function responseBodyRequirementCaptureCounts(entries) {
  return responseBodyRequirements().map(requirement => ({
    path: requirement.path,
    role: requirement.role,
    required_count: requirementRequiredCount(requirement),
    captured_count: entries.filter(entry => (
      entryMatchesRequiredPath(entry, requirement.path)
      && entrySatisfiesResponseBodyRequirement(entry, requirement)
    )).length,
  }));
}

function requirementRequiredCount(requirement) {
  const requirementCount = Number(requirement && requirement.missing_response_body_count);
  if (Number.isFinite(requirementCount) && requirementCount > 0) return Math.trunc(requirementCount);
  return requiredResponsePathCounts()[requirement.path] || 1;
}

function missingResponseBodyRequirements(captureCounts) {
  return captureCounts.flatMap(item => {
    const missingCount = item.required_count - item.captured_count;
    if (missingCount <= 0) return [];
    return [{
      path: item.path,
      role: item.role,
      missing_count: missingCount,
      required_count: item.required_count,
      captured_count: item.captured_count,
    }];
  });
}

function responseBodyRequirementRoleCaptureCounts(captureCounts) {
  return captureCounts.reduce((counts, item) => {
    const role = item.role || 'workflow_response';
    if (!counts[role]) counts[role] = { required_count: 0, captured_count: 0, missing_count: 0 };
    counts[role].required_count += item.required_count;
    counts[role].captured_count += item.captured_count;
    counts[role].missing_count += Math.max(0, item.required_count - item.captured_count);
    return counts;
  }, {});
}

function countBySafePath(urls) {
  return urls.reduce((counts, url) => {
    const safePath = safeUrlPathWithQueryKeys(url);
    counts[safePath] = (counts[safePath] || 0) + 1;
    return counts;
  }, {});
}

function sourceHarDirectPdfUrls() {
  if (!SOURCE_HAR_PATH || !fs.existsSync(SOURCE_HAR_PATH)) return [];
  try {
    const har = JSON.parse(fs.readFileSync(SOURCE_HAR_PATH, 'utf8'));
    const entries = (((har || {}).log || {}).entries || []);
    const seen = new Set();
    const urls = [];
    for (const entry of entries) {
      const request = entry && entry.request || {};
      const response = entry && entry.response || {};
      const content = response.content || {};
      const url = String(request.url || '');
      if (!url || response.status !== 200) continue;
      let parsed;
      try {
        parsed = new URL(url);
      } catch {
        continue;
      }
      if (!parsed.hostname.endsWith('bkiconnect.com')) continue;
      const safePath = safeUrlPathWithQueryKeys(url);
      const directPdfLike = isPdfMime(content.mimeType) || safePath === '/LISViewDoc.aspx?enc';
      if (!directPdfLike || seen.has(url)) continue;
      seen.add(url);
      urls.push(url);
      if (urls.length >= SOURCE_DIRECT_PDF_LIMIT) break;
    }
    return urls;
  } catch {
    return [];
  }
}

function sourceHarRequiredResponseUrls() {
  if (!SOURCE_HAR_PATH || !fs.existsSync(SOURCE_HAR_PATH)) return [];
  const requiredPaths = requiredResponsePaths();
  if (!requiredPaths.length) return [];
  try {
    const har = JSON.parse(fs.readFileSync(SOURCE_HAR_PATH, 'utf8'));
    const entries = (((har || {}).log || {}).entries || []);
    const seen = new Set();
    const urls = [];
    for (const entry of entries) {
      const request = entry && entry.request || {};
      const response = entry && entry.response || {};
      const url = String(request.url || '');
      if (!url || response.status !== 200) continue;
      let parsed;
      try {
        parsed = new URL(url);
      } catch {
        continue;
      }
      if (!parsed.hostname.endsWith('bkiconnect.com')) continue;
      const safePath = safeUrlPathWithQueryKeys(url);
      if (!requiredPaths.some(requiredPath => safePathMatchesRequiredPath(safePath, requiredPath))) continue;
      if (seen.has(url)) continue;
      seen.add(url);
      urls.push(url);
    }
    return urls;
  } catch {
    return [];
  }
}

function buildNextAction(report) {
  if (
    report.status === 'ok'
    && report.replayable_document_payload_count > 0
    && report.target_month_replayable_document_available === true
  ) {
    return {
      status: 'ready_for_visible_cdp_download',
      reason: null,
      next_command: runDownloaderCommand(),
      capture_required: false,
      required_response_paths: [],
      required_response_path_counts: {},
      required_response_path_progress: [],
      response_body_requirements: [],
    };
  }
  if (report.status === 'auth_required') {
    const targetId = report.manual_auth_target_id || TARGET_ID || '';
    return {
      status: 'capture_authenticated_tab',
      reason: report.reason || 'no_authenticated_citadel_tab',
      next_command: report.manual_auth_next_command || manualAuthCommand(targetId),
      install_verified_har_dry_run_command: report.manual_auth_install_verified_har_dry_run_command
        || manualAuthInstallDryRunCommand(targetId),
      install_verified_har_apply_command: report.manual_auth_install_verified_har_apply_command
        || manualAuthCommand(targetId),
      capture_command: CAPTURE_ONLY_COMMAND,
      capture_then_install_command: MANUAL_CAPTURE_COMMAND,
      capture_required: true,
      required_response_paths: requiredResponsePaths(),
      required_response_path_counts: requiredResponsePathCounts(),
      required_response_path_progress: report.required_response_path_progress || requiredResponsePathProgress(),
      response_body_requirements: responseBodyRequirements(),
    };
  }
  if (report.status === 'cdp_unavailable') {
    return {
      status: 'start_cdp_browser',
      reason: report.reason || 'cdp_unavailable',
      next_command: CAPTURE_ONLY_COMMAND,
      capture_command: CAPTURE_ONLY_COMMAND,
      capture_required: true,
      required_response_paths: requiredResponsePaths(),
      required_response_path_counts: requiredResponsePathCounts(),
      required_response_path_progress: report.required_response_path_progress || requiredResponsePathProgress(),
      response_body_requirements: responseBodyRequirements(),
    };
  }
  if (report.reason === 'captured_no_replayable_pdf_payloads' || report.reason === 'captured_missing_required_response_bodies') {
    return {
      status: 'capture_full_response_bodies',
      reason: report.reason,
      next_command: MANUAL_CAPTURE_COMMAND,
      capture_command: CAPTURE_ONLY_COMMAND,
      capture_required: true,
      required_response_paths: report.missing_required_response_paths && report.missing_required_response_paths.length
        ? report.missing_required_response_paths
        : requiredResponsePaths(),
      required_response_path_counts: report.missing_required_response_path_counts && Object.keys(report.missing_required_response_path_counts).length
        ? report.missing_required_response_path_counts
        : requiredResponsePathCounts(),
      required_response_path_progress: report.required_response_path_progress || requiredResponsePathProgress(),
      response_body_requirements: report.missing_response_body_requirements && report.missing_response_body_requirements.length
        ? report.missing_response_body_requirements
        : responseBodyRequirements(),
    };
  }
  if (report.reason === 'captured_target_month_statement_unavailable') {
    return {
      status: 'capture_target_month_statement',
      reason: report.reason,
      next_command: MANUAL_CAPTURE_COMMAND,
      capture_command: CAPTURE_ONLY_COMMAND,
      capture_required: true,
      target_month: report.target_month,
      target_month_replayable_document_available: report.target_month_replayable_document_available,
      target_month_replayable_document_payload_count: report.target_month_replayable_document_payload_count,
      replayable_statement_months: report.replayable_statement_months || [],
      statement_document_months: report.statement_document_months || [],
      required_response_paths: report.missing_required_response_paths && report.missing_required_response_paths.length
        ? report.missing_required_response_paths
        : requiredResponsePaths(),
      required_response_path_counts: report.missing_required_response_path_counts && Object.keys(report.missing_required_response_path_counts).length
        ? report.missing_required_response_path_counts
        : requiredResponsePathCounts(),
      required_response_path_progress: report.required_response_path_progress || requiredResponsePathProgress(),
      response_body_requirements: report.missing_response_body_requirements && report.missing_response_body_requirements.length
        ? report.missing_response_body_requirements
        : responseBodyRequirements(),
    };
  }
  return {
    status: 'inspect_cdp_capture',
    reason: report.reason || report.status || 'unknown',
    next_command: CAPTURE_ONLY_COMMAND,
    capture_command: CAPTURE_ONLY_COMMAND,
    capture_required: true,
    required_response_paths: requiredResponsePaths(),
    required_response_path_counts: requiredResponsePathCounts(),
    required_response_path_progress: report.required_response_path_progress || requiredResponsePathProgress(),
    response_body_requirements: responseBodyRequirements(),
  };
}

function writeManualAuthRequired(report, targetId) {
  const pinnedTargetId = targetId || TARGET_ID || '';
  const dryRunCommand = manualAuthInstallDryRunCommand(pinnedTargetId);
  const applyCommand = manualAuthCommand(pinnedTargetId);
  const handoff = {
    status: 'auth_required',
    reason: report.reason || 'no_authenticated_citadel_tab',
    portal_url: PORTAL_URL,
    cdp_target_id: pinnedTargetId || null,
    route_counts: report.route_counts || {},
    login_tab_count: report.login_tab_count || 0,
    non_login_tab_count: report.non_login_tab_count || 0,
    scanned_count: report.scanned_count || 0,
    candidate_count: report.candidate_count || 0,
    next_command: applyCommand,
    install_verified_har_dry_run_command: dryRunCommand,
    install_verified_har_apply_command: applyCommand,
    capture_command: CAPTURE_ONLY_COMMAND,
    capture_then_install_command: MANUAL_CAPTURE_COMMAND,
    capture_har_path: CAPTURE_HAR_PATH,
    report_path: REPORT_PATH,
    required_response_paths: requiredResponsePaths(),
    required_response_path_counts: requiredResponsePathCounts(),
    required_response_path_progress: report.required_response_path_progress || requiredResponsePathProgress(),
    response_body_requirements: responseBodyRequirements(),
    written_at: new Date().toISOString(),
  };
  writeStableJson(MANUAL_AUTH_FILE, handoff);
  report.manual_auth_file = MANUAL_AUTH_FILE;
  report.manual_auth_portal_url = PORTAL_URL;
  report.manual_auth_target_id = handoff.cdp_target_id;
  report.manual_auth_next_command = handoff.next_command;
  report.manual_auth_install_verified_har_dry_run_command = handoff.install_verified_har_dry_run_command;
  report.manual_auth_install_verified_har_apply_command = handoff.install_verified_har_apply_command;
  report.manual_auth_required_response_paths = handoff.required_response_paths;
  report.manual_auth_required_response_path_counts = handoff.required_response_path_counts;
}

async function main() {
  fs.mkdirSync(REPORTS, { recursive: true });
  const sourceDirectPdfUrls = sourceHarDirectPdfUrls();
  const sourceRequiredResponseUrls = sourceHarRequiredResponseUrls();
  const sourceRequiredResponsePathCounts = countBySafePath(sourceRequiredResponseUrls);
  const report = {
    job: 'citadel-replay-har-capture',
    started_at: new Date().toISOString(),
    status: 'unknown',
    portal_url: PORTAL_URL,
    cdp_version_url: VERSION_URL,
    capture_har_path: CAPTURE_HAR_PATH,
    capture_har_written: false,
    capture_har_write_skipped_reason: null,
    source_har_path_exists: !!(SOURCE_HAR_PATH && fs.existsSync(SOURCE_HAR_PATH)),
    source_direct_pdf_limit: SOURCE_DIRECT_PDF_LIMIT,
    source_direct_pdf_candidate_count: sourceDirectPdfUrls.length,
    source_direct_pdf_path_counts: countBySafePath(sourceDirectPdfUrls),
    source_direct_pdf_fetched_count: 0,
    source_direct_pdf_replayable_count: 0,
    source_required_response_candidate_count: sourceRequiredResponseUrls.length,
    source_required_response_path_counts: sourceRequiredResponsePathCounts,
    source_required_response_fetched_count: 0,
    source_required_response_replayable_count: 0,
    target_month: currentTargetMonth(),
    target_month_replayable_document_available: false,
    target_month_replayable_document_payload_count: 0,
    replayable_statement_months: [],
    statement_document_months: [],
    scan_limit: SCAN_LIMIT,
    scan_fetch_timeout_ms: FETCH_TIMEOUT_MS,
    cdp_command_timeout_ms: CDP_COMMAND_TIMEOUT_MS,
    scan_target_id_requested: !!TARGET_ID,
    scan_target_id_found: null,
    required_response_paths: requiredResponsePaths(),
    required_response_path_counts: requiredResponsePathCounts(),
    response_body_requirements: responseBodyRequirements(),
    response_body_requirement_role_counts: requirementRoleCounts(responseBodyRequirements()),
    captured_response_body_requirement_counts: [],
    response_body_requirement_role_capture_counts: {},
    missing_response_body_requirements: responseBodyRequirements(),
    missing_response_body_requirement_count: responseBodyRequirements().length,
    captured_required_response_paths: [],
    captured_required_response_path_counts: {},
    required_response_path_progress: requiredResponsePathProgress({}, sourceRequiredResponsePathCounts),
    missing_required_response_paths: requiredResponsePaths(),
    missing_required_response_path_counts: requiredResponsePathCounts(),
    candidate_count: 0,
    route_counts: {},
    login_tab_count: 0,
    non_login_tab_count: 0,
    scanned_count: 0,
    authenticated_found: false,
    captured_endpoint_count: 0,
    captured_response_body_count: 0,
    captured_replayable_response_body_count: 0,
    statement_candidate_count: 0,
    replayable_document_payload_count: 0,
    direct_pdf_response_count: 0,
    statuses: [],
    errors: [],
    manual_auth_required: false,
    manual_auth_file: null,
    manual_auth_portal_url: null,
    manual_auth_target_id: null,
    manual_auth_next_command: null,
  };

  let ws = null;
  let id = 0;
  const pending = new Map();
  function send(method, params = {}, sessionId, timeoutMs = CDP_COMMAND_TIMEOUT_MS) {
    const msg = { id: ++id, method, params };
    if (sessionId) msg.sessionId = sessionId;
    ws.send(JSON.stringify(msg));
    return new Promise((resolve, reject) => {
      const timeout = Math.max(1, Math.trunc(timeoutMs));
      const timer = setTimeout(() => {
        pending.delete(msg.id);
        reject(new Error(`CDP command timed out: ${method}`));
      }, timeout);
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

  async function fetchInSession(sessionId, url, cdpTimeoutMs = CDP_COMMAND_TIMEOUT_MS, fetchTimeoutMs = FETCH_TIMEOUT_MS) {
    const runtimeFetchTimeoutMs = Math.max(1, Math.trunc(fetchTimeoutMs));
    const evaluated = await send('Runtime.evaluate', {
      awaitPromise: true,
      returnByValue: true,
      expression: `
        (async () => {
          try {
            ${storageAuthHelperSource()}
            const auth = __citadelStorageAuth();
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), ${runtimeFetchTimeoutMs});
            const headers = { accept: 'application/json', 'x-correlation-id': String(crypto.randomUUID ? crypto.randomUUID() : Date.now()) };
            if (auth.mobileSourceId) headers['x-mobile-source-id'] = auth.mobileSourceId;
            if (auth.bearerToken) headers.authorization = 'Bearer ' + auth.bearerToken;
            const resp = await fetch(${JSON.stringify(url)}, {
              credentials: 'include',
              headers,
              signal: controller.signal
            });
            const contentType = resp.headers.get('content-type') || 'application/json';
            let text = '';
            let contentEncoding = '';
            let bodySize = 0;
            if (contentType.toLowerCase().split(';')[0].trim() === 'application/pdf') {
              const buf = await resp.arrayBuffer();
              const bytes = new Uint8Array(buf);
              bodySize = bytes.length;
              let binary = '';
              for (let i = 0; i < bytes.length; i += 0x8000) {
                binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
              }
              text = btoa(binary);
              contentEncoding = 'base64';
            } else {
              text = await resp.text();
              bodySize = text.length;
            }
            clearTimeout(timer);
            return {
              ok: resp.ok,
              status: resp.status,
              status_text: resp.statusText,
              content_type: contentType,
              content_encoding: contentEncoding,
              body_size: bodySize,
              text,
              text_len: text.length,
              token_candidate_count: auth.tokenCandidateCount,
              bearer_token_available: !!auth.bearerToken,
              mobile_source_id_available: !!auth.mobileSourceId
            };
          } catch (e) {
            return { ok: false, status: 0, error: e.message, text: '', text_len: 0 };
          }
        })()
      `,
    }, sessionId, cdpTimeoutMs);
    return evaluated && evaluated.result && evaluated.result.value || { ok: false, status: 0, text: '' };
  }

  try {
    const version = await (await fetch(VERSION_URL)).json();
    report.browser = version.Browser || null;
    ws = new WebSocket(version.webSocketDebuggerUrl);
    ws.onmessage = ev => {
      const msg = JSON.parse(ev.data);
      if (!msg.id) return;
      const waiter = pending.get(msg.id);
      if (!waiter) return;
      pending.delete(msg.id);
      if (msg.error) waiter.reject(new Error(JSON.stringify(msg.error)));
      else waiter.resolve(msg.result);
    };
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('CDP websocket open timed out')), 15000);
      ws.onopen = () => {
        clearTimeout(timer);
        resolve();
      };
      ws.onerror = error => {
        clearTimeout(timer);
        reject(error);
      };
    });

    let selected = null;
    const started = Date.now();
    const deadline = started + AUTH_WAIT_MS;
    const remainingAuthMs = () => Math.max(0, deadline - Date.now());
    const authCommandTimeoutMs = () => Math.min(CDP_COMMAND_TIMEOUT_MS, Math.max(1, remainingAuthMs()));
    const authFetchTimeoutMs = () => Math.min(FETCH_TIMEOUT_MS, Math.max(1, remainingAuthMs()));
    while (!selected && remainingAuthMs() > 0) {
      const targets = await send('Target.getTargets', {}, undefined, authCommandTimeoutMs());
      const candidates = prioritize((targets.targetInfos || []).filter(t => t.type === 'page' && t.url && t.url.includes('bkiconnect.com')));
      report.candidate_count = candidates.length;
      report.route_counts = routeCounts(candidates);
      report.login_tab_count = candidates.filter(t => String(t.url || '').includes('#/login')).length;
      report.non_login_tab_count = Math.max(0, candidates.length - report.login_tab_count);
      report.scan_target_id_found = TARGET_ID ? candidates.some(t => t.targetId === TARGET_ID) : null;
      for (const candidate of candidates.slice(0, SCAN_LIMIT)) {
        if (remainingAuthMs() <= 0) break;
        report.scanned_count += 1;
        let sessionId = '';
        try {
          const attached = await send('Target.attachToTarget', { targetId: candidate.targetId, flatten: true }, undefined, authCommandTimeoutMs());
          sessionId = attached.sessionId;
          await send('Runtime.enable', {}, sessionId, authCommandTimeoutMs());
          const summary = await fetchInSession(
            sessionId,
            `${API_BASE}/sd/loan-info/v2/my/loans-summary`,
            authCommandTimeoutMs(),
            authFetchTimeoutMs(),
          );
          report.statuses.push({
            target_id: candidate.targetId,
            url: candidate.url,
            title: candidate.title || null,
            ok: !!summary.ok,
            status: summary.status || null,
            text_len: summary.text_len || 0,
            bearer_token_available: !!summary.bearer_token_available,
            mobile_source_id_available: !!summary.mobile_source_id_available,
            token_candidate_count: summary.token_candidate_count || 0,
            error: summary.error ? String(summary.error).slice(0, 160) : null,
          });
          if (summary.ok) {
            selected = { targetId: candidate.targetId, sessionId, summary };
            break;
          }
        } catch (error) {
          report.statuses.push({
            target_id: candidate.targetId,
            url: candidate.url,
            title: candidate.title || null,
            ok: false,
            error: String(error && error.message || error).slice(0, 160),
          });
        } finally {
          if (!selected && sessionId) await send('Target.detachFromTarget', { sessionId }).catch(() => null);
        }
      }
      if (!selected && remainingAuthMs() > 0) {
        await new Promise(resolve => setTimeout(resolve, Math.min(2000, remainingAuthMs())));
      }
    }

    if (!selected) {
      report.status = 'auth_required';
      report.manual_auth_required = true;
      report.reason = report.candidate_count ? 'no_authenticated_citadel_tab' : 'no_citadel_tab';
      const scanned = report.statuses.find(item => item && item.target_id);
      writeManualAuthRequired(report, scanned && scanned.target_id);
      return;
    }

    report.authenticated_found = true;
    report.authenticated_target_id = selected.targetId;
    const entries = [harEntry(`${API_BASE}/sd/loan-info/v2/my/loans-summary`, selected.summary)];
    const fetchedUrls = new Set([`${API_BASE}/sd/loan-info/v2/my/loans-summary`]);

    const eStatementsUrl = `${API_BASE}/sd/loan-maint/v2/my/loans/${LOAN_ORG}/${LOAN_ID}/eStatements`;
    const docsUrl = `${API_BASE}/lsd/api/v1/my/loans/${LOAN_ORG}/${LOAN_ID}/docs`;
    const eStatements = await fetchInSession(selected.sessionId, eStatementsUrl);
    const docs = await fetchInSession(selected.sessionId, docsUrl);
    entries.push(harEntry(eStatementsUrl, eStatements));
    entries.push(harEntry(docsUrl, docs));
    fetchedUrls.add(eStatementsUrl);
    fetchedUrls.add(docsUrl);

    const statementDocs = parseList(docs.text, ['Documents', 'documents', 'docs', 'Docs', 'data', 'Data']).filter(isStatementDocument);
    report.statement_candidate_count = statementDocs.length;
    for (const doc of statementDocs.slice(0, DOC_LIMIT)) {
      addUnique(report.statement_document_months, statementMonthFromDoc(doc));
      const docId = docField(doc, 'id', 'Id', 'ID', 'documentId', 'DocumentId', 'DocumentID', 'docId', 'DocId', 'DocumentIdentifier');
      if (!docId) continue;
      const docUrl = `${API_BASE}/lsd/api/v1/my/loans/${LOAN_ORG}/${LOAN_ID}/docs/${encodeURIComponent(String(docId))}`;
      const detail = await fetchInSession(selected.sessionId, docUrl);
      entries.push(harEntry(docUrl, detail));
      fetchedUrls.add(docUrl);
      const replayableCount = replayablePayloadCountFromResult(detail);
      const detailJson = jsonFromResult(detail);
      const statementMonth = statementMonthFromDoc(detailJson) || statementMonthFromDoc(doc);
      report.replayable_document_payload_count += replayableCount;
      if (replayableCount > 0) {
        addUnique(report.replayable_statement_months, statementMonth);
        if (statementMonth && statementMonth === report.target_month) {
          report.target_month_replayable_document_payload_count += replayableCount;
        }
      }
      if (isPdfMime(detail && detail.content_type) && detail.content_encoding === 'base64') {
        report.direct_pdf_response_count += 1;
      }
    }

    for (const requiredUrl of sourceRequiredResponseUrls) {
      if (fetchedUrls.has(requiredUrl)) continue;
      const requiredResult = await fetchInSession(selected.sessionId, requiredUrl);
      entries.push(harEntry(requiredUrl, requiredResult));
      fetchedUrls.add(requiredUrl);
      report.source_required_response_fetched_count += 1;
      const replayableCount = replayablePayloadCountFromResult(requiredResult);
      const statementMonth = statementMonthFromDoc(jsonFromResult(requiredResult));
      report.replayable_document_payload_count += replayableCount;
      report.source_required_response_replayable_count += replayableCount;
      if (replayableCount > 0) {
        addUnique(report.replayable_statement_months, statementMonth);
        if (statementMonth && statementMonth === report.target_month) {
          report.target_month_replayable_document_payload_count += replayableCount;
        }
      }
      if (isPdfMime(requiredResult && requiredResult.content_type) && requiredResult.content_encoding === 'base64') {
        report.direct_pdf_response_count += 1;
      }
    }

    for (const pdfUrl of sourceDirectPdfUrls) {
      if (fetchedUrls.has(pdfUrl)) continue;
      const pdfResult = await fetchInSession(selected.sessionId, pdfUrl);
      entries.push(harEntry(pdfUrl, pdfResult));
      fetchedUrls.add(pdfUrl);
      report.source_direct_pdf_fetched_count += 1;
      const replayableCount = replayablePayloadCountFromResult(pdfResult);
      report.replayable_document_payload_count += replayableCount;
      report.source_direct_pdf_replayable_count += replayableCount;
      if (isPdfMime(pdfResult && pdfResult.content_type) && pdfResult.content_encoding === 'base64') {
        report.direct_pdf_response_count += 1;
      }
    }

    const har = {
      log: {
        version: '1.2',
        creator: { name: 'openclaw-citadel-capture', version: '1' },
        pages: [],
        entries,
      },
    };
    report.captured_required_response_path_counts = capturedRequiredResponsePathCounts(entries);
    report.missing_required_response_path_counts = missingRequiredResponsePathCounts(report.captured_required_response_path_counts);
    report.captured_required_response_paths = capturedRequiredResponsePaths(entries);
    report.required_response_path_progress = requiredResponsePathProgress(
      report.captured_required_response_path_counts,
      report.source_required_response_path_counts,
    );
    report.missing_required_response_paths = Object.keys(report.missing_required_response_path_counts);
    report.captured_response_body_requirement_counts = responseBodyRequirementCaptureCounts(entries);
    report.response_body_requirement_role_capture_counts = responseBodyRequirementRoleCaptureCounts(report.captured_response_body_requirement_counts);
    report.missing_response_body_requirements = missingResponseBodyRequirements(report.captured_response_body_requirement_counts);
    report.missing_response_body_requirement_count = report.missing_response_body_requirements.length;
    report.captured_endpoint_count = entries.length;
    report.captured_response_body_count = entries.filter(entry => entry.response && entry.response.content && entry.response.content.text).length;
    report.captured_replayable_response_body_count = entries.filter(entry => entrySatisfiesResponseBodyRequirement(entry)).length;
    if (
      report.direct_pdf_response_count > 0
      && report.statement_document_months.includes(report.target_month)
      && report.target_month_replayable_document_payload_count === 0
    ) {
      report.target_month_replayable_document_payload_count += report.direct_pdf_response_count;
      addUnique(report.replayable_statement_months, report.target_month);
    }
    report.target_month_replayable_document_available = report.target_month_replayable_document_payload_count > 0;
    const hasTargetMonthPayload = report.target_month_replayable_document_available;
    report.status = hasTargetMonthPayload && report.missing_required_response_paths.length === 0 ? 'ok' : 'review';
    if (report.status === 'review') {
      if (report.missing_required_response_paths.length) {
        report.reason = 'captured_missing_required_response_bodies';
      } else if (report.replayable_document_payload_count > 0 && !hasTargetMonthPayload) {
        report.reason = 'captured_target_month_statement_unavailable';
      } else {
        report.reason = 'captured_no_replayable_pdf_payloads';
      }
    }
    const harInstallDecision = captureHarInstallDecision(report);
    if (harInstallDecision.ok) {
      writeStableJson(CAPTURE_HAR_PATH, har, VOLATILE_HAR_FIELDS);
      report.capture_har_written = true;
      report.capture_har_write_skipped_reason = null;
    } else {
      report.capture_har_written = false;
      report.capture_har_write_skipped_reason = harInstallDecision.reason;
    }
  } catch (error) {
    const message = String(error && error.message || error);
    if (message.includes('CDP command timed out: Target.getTargets')) {
      report.status = 'cdp_unavailable';
      report.reason = 'cdp_target_scan_timeout';
      report.manual_auth_required = true;
    } else {
      report.status = 'error';
      report.reason = 'unexpected_cdp_capture_error';
    }
    report.errors.push(message.slice(0, 300));
  } finally {
    if (report.capture_har_written !== true && !report.capture_har_write_skipped_reason) {
      report.capture_har_write_skipped_reason = report.reason || report.status || 'capture_not_ready';
    }
    report.ended_at = new Date().toISOString();
    report.next_action = buildNextAction(report);
    report.next_command = report.next_action.next_command;
    writeStableJson(REPORT_PATH, report);
    if (ws) ws.close();
    console.log(`[citadel-capture] ${report.status} authenticated=${report.authenticated_found} endpoints=${report.captured_endpoint_count} replayable=${report.replayable_document_payload_count}`);
    process.exitCode = report.status === 'ok' ? 0 : 1;
  }
}

main();
