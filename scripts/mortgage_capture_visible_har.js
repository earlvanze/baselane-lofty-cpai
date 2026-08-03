#!/usr/bin/env node
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const WebSocket = require('./node_modules/ws');

const ROOT = process.env.WORKSPACE_ROOT || path.join(process.env.HOME || '', '.openclaw', 'workspace');
const CDP_JSON_BASE = (process.env.MORTGAGE_WORKFLOW_CDP_JSON_BASE || process.env.BASELANE_CDP_JSON_BASE || 'http://127.0.0.1:9222').replace(/\/$/, '');
const TARGET_ID = (process.env.MORTGAGE_WORKFLOW_TAB_SCAN_TARGET_ID || '').trim();
const PROPERTY = (process.env.MORTGAGE_WORKFLOW_PROPERTY || '').trim();
const PORTAL_URL = (process.env.MORTGAGE_WORKFLOW_PORTAL_URL || '').trim();
const HAR_PATH = process.env.MORTGAGE_WORKFLOW_HAR_PATH || path.join(ROOT, 'reports', 'mortgage_workflow_capture.har');
const AUTH_WAIT_MS = positiveInt(process.env.MORTGAGE_WORKFLOW_AUTH_WAIT_MS, 300000);
const COMMAND_TIMEOUT_MS = positiveInt(process.env.MORTGAGE_WORKFLOW_CDP_COMMAND_TIMEOUT_MS, 15000);
const BODY_DRAIN_MS = nonNegativeInt(process.env.MORTGAGE_WORKFLOW_BODY_DRAIN_MS, 5000);
const MIN_RESPONSE_BODY_ENTRIES = nonNegativeInt(process.env.MORTGAGE_WORKFLOW_MIN_RESPONSE_BODY_ENTRIES, 1);
const REQUIRED_RESPONSE_PATHS = csvList(process.env.MORTGAGE_WORKFLOW_REQUIRED_RESPONSE_PATHS || '');
const EXPECTED_DOCUMENT_IDS = csvList(process.env.MORTGAGE_WORKFLOW_EXPECTED_DOCUMENT_IDS || '');

function positiveInt(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.trunc(parsed) : fallback;
}

function nonNegativeInt(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? Math.trunc(parsed) : fallback;
}

function csvList(value) {
  return String(value || '')
    .split(',')
    .map(item => item.trim())
    .filter(Boolean);
}

function fetchJson(url) {
  return fetch(url).then(response => {
    if (!response.ok) throw new Error(`HTTP ${response.status} from ${url}`);
    return response.json();
  });
}

const SENSITIVE_HEADER_NAMES = new Set([
  'authorization',
  'authorization-provider',
  'b2ctoken',
  'cookie',
  'hostinfo',
  'macvisitorid',
  'ocp-apim-subscription-key',
  'proxy-authorization',
  'set-cookie',
  'subscription-key',
  'x-api-key',
  'x-apim-subscription-key',
  'x-auth-token',
  'x-csrf-token',
  'x-ms-cpim-csrf',
  'x-partner-id',
  'x-xsrf-token',
]);

function headerValue(headers, name) {
  if (!headers || typeof headers !== 'object') return '';
  const target = String(name || '').toLowerCase();
  for (const [key, value] of Object.entries(headers)) {
    if (String(key).toLowerCase() === target) return String(value || '');
  }
  return '';
}

function sanitizedHeaderPairs(headers) {
  if (!headers || typeof headers !== 'object') return [];
  return Object.entries(headers).map(([name, value]) => {
    const key = String(name);
    const lowerKey = key.toLowerCase();
    return {
      name: key,
      value: SENSITIVE_HEADER_NAMES.has(lowerKey) ? '[REDACTED]' : String(value),
    };
  });
}

function redactsBodyKey(key) {
  const normalized = String(key || '').replace(/[^a-z0-9]/gi, '').toLowerCase();
  if (['code', 'pin'].includes(normalized)) return true;
  return [
    'authorization',
    'bearer',
    'clientsecret',
    'cookie',
    'mfa',
    'otp',
    'passcode',
    'password',
    'requestuuid',
    'secret',
    'session',
    'token',
    'verificationcode',
  ].some(part => normalized.includes(part));
}

function sanitizeStructuredBody(value) {
  if (Array.isArray(value)) return value.map(sanitizeStructuredBody);
  if (!value || typeof value !== 'object') return value;
  const sanitized = {};
  for (const [key, item] of Object.entries(value)) {
    sanitized[key] = redactsBodyKey(key) ? '[REDACTED]' : sanitizeStructuredBody(item);
  }
  return sanitized;
}

function sanitizeUrlEncodedPostData(text) {
  const params = new URLSearchParams(text);
  const sanitized = new URLSearchParams();
  for (const [key, value] of params.entries()) {
    sanitized.append(key, redactsBodyKey(key) ? '[REDACTED]' : value);
  }
  return sanitized.toString();
}

function sanitizeRequestPostData(postData, contentType = '') {
  const text = String(postData || '');
  if (!text) return '';
  const normalizedType = String(contentType || '').toLowerCase();
  const trimmed = text.trim();
  if (normalizedType.includes('application/json') || trimmed.startsWith('{') || trimmed.startsWith('[')) {
    try {
      return JSON.stringify(sanitizeStructuredBody(JSON.parse(text)));
    } catch {
      return '[REDACTED_POST_BODY]';
    }
  }
  if (normalizedType.includes('application/x-www-form-urlencoded') || /^[^=&]+=[\s\S]*(&[^=&]+=[\s\S]*)*$/.test(text)) {
    try {
      return sanitizeUrlEncodedPostData(text);
    } catch {
      return '[REDACTED_POST_BODY]';
    }
  }
  return '[REDACTED_POST_BODY]';
}

function contentSize(body) {
  if (!body) return 0;
  return body.base64Encoded ? Buffer.byteLength(body.text || '', 'base64') : Buffer.byteLength(body.text || '', 'utf8');
}

function buildHarEntry(record) {
  const request = record.request || {};
  const response = record.response || {};
  const body = record.body || {};
  const mimeType = (response.mimeType || (response.headers && response.headers['content-type']) || '').split(';')[0];
  const text = body.text || '';
  const size = contentSize(body);
  const requestContentType = headerValue(request.headers, 'content-type');
  const sanitizedPostData = request.postData ? sanitizeRequestPostData(request.postData, requestContentType) : '';
  return {
    startedDateTime: record.startedDateTime || new Date().toISOString(),
    time: Math.max(0, (record.endedAt || Date.now()) - (record.startedAt || Date.now())),
    request: {
      method: request.method || 'GET',
      url: request.url || '',
      httpVersion: 'HTTP/2',
      cookies: [],
      headers: sanitizedHeaderPairs(request.headers),
      queryString: [],
      headersSize: -1,
      bodySize: request.postData ? Buffer.byteLength(String(request.postData), 'utf8') : 0,
      ...(request.postData ? { postData: { mimeType: requestContentType, text: sanitizedPostData } } : {}),
    },
    response: {
      status: response.status || 0,
      statusText: response.statusText || '',
      httpVersion: 'HTTP/2',
      cookies: [],
      headers: sanitizedHeaderPairs(response.headers),
      content: {
        size,
        mimeType: mimeType || response.mimeType || '',
        ...(text ? { text } : {}),
        ...(body.base64Encoded ? { encoding: 'base64' } : {}),
      },
      redirectURL: '',
      headersSize: -1,
      bodySize: size || -1,
    },
    cache: {},
    timings: { send: 0, wait: -1, receive: 0 },
    _property: PROPERTY,
    _body_capture_error: record.bodyError || undefined,
    _loading_failed: record.loadingFailed || undefined,
  };
}

function buildHarReport(records) {
  const entries = Array.from(records.values())
    .filter(record => record.request && record.request.url && record.response)
    .sort((a, b) => (a.startedAt || 0) - (b.startedAt || 0))
    .map(buildHarEntry);
  validateHarEntries(entries, MIN_RESPONSE_BODY_ENTRIES, REQUIRED_RESPONSE_PATHS, EXPECTED_DOCUMENT_IDS);
  return {
    log: {
      version: '1.2',
      creator: { name: 'mortgage_capture_visible_har.js', version: '1' },
      pages: [],
      entries,
    },
  };
}

function entryPath(entry) {
  try {
    return new URL((entry.request || {}).url || '').pathname;
  } catch {
    return '';
  }
}

function entryMatchesRequiredPath(entry, requiredPath) {
  const rawRequiredPath = String(requiredPath || '').trim();
  if (!rawRequiredPath) return false;
  let url;
  try {
    url = new URL((entry.request || {}).url || '');
  } catch {
    return false;
  }
  const [pathPart, queryPart = ''] = rawRequiredPath.split('?', 2);
  if (url.pathname !== pathPart) return false;
  if (!queryPart) return true;
  const requiredKeys = queryPart
    .split('&')
    .map(item => item.split('=', 1)[0].trim())
    .filter(Boolean);
  return requiredKeys.every(key => url.searchParams.has(key));
}

function entryHasBody(entry) {
  return Boolean(entry && entry.response && entry.response.content && entry.response.content.text);
}

function entryBodyText(entry) {
  const content = entry && entry.response && entry.response.content;
  if (!content || !content.text) return '';
  const text = String(content.text || '');
  if (content.encoding === 'base64') {
    try {
      return Buffer.from(text, 'base64').toString('utf8');
    } catch {
      return '';
    }
  }
  return text;
}

function entrySearchText(entry) {
  const request = (entry && entry.request) || {};
  const postData = request.postData && request.postData.text ? request.postData.text : '';
  return [
    request.url || '',
    postData,
    entryBodyText(entry),
  ].join('\n');
}

function validateHarEntries(
  entries,
  minResponseBodyEntries = MIN_RESPONSE_BODY_ENTRIES,
  requiredResponsePaths = [],
  expectedDocumentIds = []
) {
  if (!Array.isArray(entries) || entries.length === 0) {
    throw new Error('No completed network responses were captured; leaving the HAR target unchanged.');
  }
  const bodyEntryCount = entries.filter(entryHasBody).length;
  if (minResponseBodyEntries > 0 && bodyEntryCount < minResponseBodyEntries) {
    throw new Error(`Captured ${entries.length} network responses but only ${bodyEntryCount} response bodies; leaving the HAR target unchanged.`);
  }
  const requiredPaths = Array.isArray(requiredResponsePaths) ? requiredResponsePaths.filter(Boolean) : [];
  const missingRequiredPaths = requiredPaths.filter(requiredPath => {
    return !entries.some(entry => entryMatchesRequiredPath(entry, requiredPath) && entryHasBody(entry));
  });
  if (missingRequiredPaths.length > 0) {
    throw new Error(
      `Captured ${entries.length} network responses but missing response bodies for required path(s): ${missingRequiredPaths.join(', ')}; leaving the HAR target unchanged.`
    );
  }
  const expectedIds = Array.isArray(expectedDocumentIds) ? expectedDocumentIds.filter(Boolean) : [];
  const missingExpectedIds = expectedIds.filter(expectedId => {
    return !entries.some(entry => entryHasBody(entry) && entrySearchText(entry).includes(String(expectedId)));
  });
  if (missingExpectedIds.length > 0) {
    throw new Error(
      `Captured ${entries.length} network responses but missing expected target document ID(s): ${missingExpectedIds.join(', ')}; leaving the HAR target unchanged.`
    );
  }
}

async function findTargetWebSocket() {
  const tabs = await fetchJson(`${CDP_JSON_BASE}/json/list`);
  const byId = tabs.find(tab => tab.id === TARGET_ID);
  if (byId && byId.webSocketDebuggerUrl) return byId.webSocketDebuggerUrl;
  const byPortal = tabs.find(tab => String(tab.url || '').startsWith(PORTAL_URL) && tab.webSocketDebuggerUrl);
  if (byPortal) return byPortal.webSocketDebuggerUrl;
  throw new Error(`Unable to find CDP target ${TARGET_ID || '(empty)'}`);
}

async function main() {
  if (!PROPERTY || !PORTAL_URL || !HAR_PATH) {
    throw new Error('MORTGAGE_WORKFLOW_PROPERTY, MORTGAGE_WORKFLOW_PORTAL_URL, and MORTGAGE_WORKFLOW_HAR_PATH are required');
  }
  const wsUrl = await findTargetWebSocket();
  const ws = new WebSocket(wsUrl);
  const pending = new Map();
  const records = new Map();
  const bodyCaptures = new Set();
  let commandId = 0;

  function send(method, params = {}) {
    const id = ++commandId;
    ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error(`CDP command timed out: ${method}`));
      }, COMMAND_TIMEOUT_MS);
      pending.set(id, { resolve, reject, timer });
    });
  }

  ws.on('message', async data => {
    const msg = JSON.parse(String(data));
    if (msg.id && pending.has(msg.id)) {
      const item = pending.get(msg.id);
      pending.delete(msg.id);
      clearTimeout(item.timer);
      if (msg.error) item.reject(new Error(msg.error.message || JSON.stringify(msg.error)));
      else item.resolve(msg.result || {});
      return;
    }
    if (msg.method === 'Network.requestWillBeSent') {
      const p = msg.params || {};
      records.set(p.requestId, {
        requestId: p.requestId,
        startedAt: Date.now(),
        startedDateTime: p.wallTime ? new Date(p.wallTime * 1000).toISOString() : new Date().toISOString(),
        request: p.request || {},
      });
    } else if (msg.method === 'Network.responseReceived') {
      const p = msg.params || {};
      const record = records.get(p.requestId) || { requestId: p.requestId, startedAt: Date.now(), startedDateTime: new Date().toISOString() };
      record.response = p.response || {};
      records.set(p.requestId, record);
    } else if (msg.method === 'Network.loadingFinished') {
      const p = msg.params || {};
      const record = records.get(p.requestId);
      if (!record || !record.response) return;
      record.endedAt = Date.now();
      const capture = (async () => {
        try {
          record.body = await send('Network.getResponseBody', { requestId: p.requestId });
        } catch (error) {
          record.bodyError = error.message;
        }
      })().finally(() => bodyCaptures.delete(capture));
      bodyCaptures.add(capture);
    } else if (msg.method === 'Network.loadingFailed') {
      const p = msg.params || {};
      const record = records.get(p.requestId) || { requestId: p.requestId, startedAt: Date.now(), startedDateTime: new Date().toISOString() };
      record.endedAt = Date.now();
      record.loadingFailed = p.errorText || 'loading_failed';
      records.set(p.requestId, record);
    }
  });

  await new Promise((resolve, reject) => {
    ws.once('open', resolve);
    ws.once('error', reject);
  });
  await send('Network.enable', { maxTotalBufferSize: 100000000, maxResourceBufferSize: 50000000 });
  await send('Page.enable').catch(() => {});
  console.error(`[mortgage] Recording visible-browser network traffic for ${Math.round(AUTH_WAIT_MS / 1000)}s.`);
  console.error('[mortgage] Log in, open the statement list, and open/download the target statement PDF before the timer ends.');
  await new Promise(resolve => setTimeout(resolve, AUTH_WAIT_MS));
  if (bodyCaptures.size > 0 && BODY_DRAIN_MS > 0) {
    console.error(`[mortgage] Waiting up to ${Math.round(BODY_DRAIN_MS / 1000)}s for ${bodyCaptures.size} response body capture(s) to finish.`);
    await Promise.race([
      Promise.allSettled(Array.from(bodyCaptures)),
      new Promise(resolve => setTimeout(resolve, BODY_DRAIN_MS)),
    ]);
  }

  const report = buildHarReport(records);
  fs.mkdirSync(path.dirname(HAR_PATH), { recursive: true });
  const tempPath = `${HAR_PATH}.tmp-${process.pid}-${crypto.randomUUID()}`;
  fs.writeFileSync(tempPath, `${JSON.stringify(report, null, 2)}\n`, { mode: 0o600 });
  fs.renameSync(tempPath, HAR_PATH);
  ws.close();
  console.error(`[mortgage] Wrote ${report.log.entries.length} HAR entries to ${HAR_PATH}`);
}

if (require.main === module) {
  main().catch(error => {
    console.error(`[mortgage] ${error.message}`);
    process.exit(1);
  });
}

module.exports = {
  buildHarEntry,
  buildHarReport,
  entryMatchesRequiredPath,
  entrySearchText,
  sanitizeRequestPostData,
  sanitizedHeaderPairs,
  entryPath,
  validateHarEntries,
};
