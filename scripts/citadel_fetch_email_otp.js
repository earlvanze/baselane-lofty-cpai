#!/usr/bin/env node
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const https = require('https');
const { execFileSync } = require('child_process');

const OPENCLAW_ROOT = process.env.OPENCLAW_ROOT || path.join(process.env.HOME || '', '.openclaw');
const ROOT = process.env.WORKSPACE_ROOT || path.join(OPENCLAW_ROOT, 'workspace');
const REPORTS = path.join(ROOT, 'reports');
const OTP_FILE = process.env.CITADEL_OTP_FILE || path.join(REPORTS, 'citadel_otp.txt');
const REPORT_PATH = process.env.CITADEL_OTP_GWS_REPORT || path.join(REPORTS, 'citadel_email_otp_report.json');
const GWS = process.env.GWS_BIN || '/home/digit/.local/bin/gws';
const GWS_CONFIG_BASE = process.env.GWS_CONFIG_BASE || path.join(OPENCLAW_ROOT, 'gws');
const GWS_KEYRING_BACKEND = process.env.GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND || 'file';
const GWS_PROJECT = process.env.GWS_PROJECT || process.env.GOOGLE_WORKSPACE_PROJECT_ID || 'sacred-result-442018-v2';
const GWS_SA_SCOPE = process.env.GWS_SA_SCOPE || 'https://www.googleapis.com/auth/gmail.modify';
const DEFAULT_ACCOUNTS = 'earl@earlbnb.com,ecosystemspm@gmail.com,earlvanze@gmail.com';
const ACCOUNTS = (process.env.CITADEL_OTP_GWS_ACCOUNTS || DEFAULT_ACCOUNTS)
  .split(',')
  .map(s => s.trim())
  .filter(Boolean);
const QUERY = process.env.CITADEL_OTP_GWS_QUERY || [
  'newer_than:1d',
  '(from:support@mycitadelservicing.com OR Citadel OR LoanSphere OR bkiconnect OR "verification code" OR "one-time" OR "one time" OR OTP OR PIN)',
].join(' ');
const MAX_RESULTS = Number(process.env.CITADEL_OTP_GWS_MAX_RESULTS || 10);
const AFTER_EPOCH_MS = Number(process.env.CITADEL_OTP_GWS_AFTER_EPOCH_MS || 0);

const AUTH_MAP = {
  'ecosystemspm@gmail.com': 'gws_oauth',
  'earlvanze@gmail.com': 'gws_oauth',
  'earl@earlbnb.com': 'gws_sa',
};

function accountSafe(account) {
  return account.replace('@', '-').replace(/\./g, '-');
}

function serviceAccountKeyPath(account) {
  return process.env.GWS_SA_KEY
    || process.env.GWS_IMPERSONATION_CREDENTIALS
    || path.join(ROOT, 'secure', `gws-service-account-${accountSafe(account)}.json`);
}

function gwsConfigDir(account) {
  const key = `GWS_CONFIG_DIR_${accountSafe(account).toUpperCase().replace(/-/g, '_')}`;
  return process.env[key] || path.join(GWS_CONFIG_BASE, accountSafe(account));
}

function parseJsonOutput(text) {
  const raw = String(text || '').trim();
  try {
    return JSON.parse(raw);
  } catch {
    const match = raw.match(/(\{[\s\S]*\}|\[[\s\S]*\])\s*$/);
    if (!match) throw new Error('gws output did not contain JSON');
    return JSON.parse(match[1]);
  }
}

function request(opts, body) {
  return new Promise((resolve, reject) => {
    const req = https.request(opts, res => {
      let data = '';
      res.on('data', chunk => { data += chunk; });
      res.on('end', () => resolve({ status: res.statusCode, body: data }));
    });
    req.on('error', reject);
    if (body) req.write(body);
    req.end();
  });
}

function b64url(value) {
  return Buffer.from(typeof value === 'string' ? value : JSON.stringify(value))
    .toString('base64')
    .replace(/=/g, '')
    .replace(/\+/g, '-')
    .replace(/\//g, '_');
}

async function serviceAccountAccessToken(account) {
  const keyPath = serviceAccountKeyPath(account);
  const sa = JSON.parse(fs.readFileSync(keyPath, 'utf8'));
  const now = Math.floor(Date.now() / 1000);
  const claim = {
    iss: sa.client_email,
    scope: GWS_SA_SCOPE,
    aud: 'https://oauth2.googleapis.com/token',
    iat: now,
    exp: now + 3600,
    sub: account,
  };
  const input = `${b64url({ alg: 'RS256', typ: 'JWT' })}.${b64url(claim)}`;
  const signature = crypto.sign('RSA-SHA256', Buffer.from(input), sa.private_key)
    .toString('base64')
    .replace(/=/g, '')
    .replace(/\+/g, '-')
    .replace(/\//g, '_');
  const body = new URLSearchParams({
    grant_type: 'urn:ietf:params:oauth:grant-type:jwt-bearer',
    assertion: `${input}.${signature}`,
  }).toString();
  const resp = await request({
    method: 'POST',
    hostname: 'oauth2.googleapis.com',
    path: '/token',
    headers: {
      'content-type': 'application/x-www-form-urlencoded',
      'content-length': Buffer.byteLength(body),
    },
  }, body);
  if (resp.status !== 200) throw new Error(`service account token failed: ${resp.status}`);
  return JSON.parse(resp.body).access_token;
}

async function gmailGet(accessToken, account, apiPath) {
  const resp = await request({
    method: 'GET',
    hostname: 'gmail.googleapis.com',
    path: apiPath,
    headers: { authorization: `Bearer ${accessToken}` },
  });
  if (resp.status !== 200) throw new Error(`gmail request failed: ${resp.status}`);
  return JSON.parse(resp.body);
}

async function listMessagesSa(account) {
  const accessToken = await serviceAccountAccessToken(account);
  const listPath = `/gmail/v1/users/${encodeURIComponent(account)}/messages?maxResults=${MAX_RESULTS}&q=${encodeURIComponent(QUERY)}`;
  const list = await gmailGet(accessToken, account, listPath);
  const messages = [];
  for (const row of (list.messages || []).slice(0, MAX_RESULTS)) {
    const msg = await gmailGet(
      accessToken,
      account,
      `/gmail/v1/users/${encodeURIComponent(account)}/messages/${encodeURIComponent(row.id)}?format=full`
    );
    messages.push(msg);
  }
  return { resultSizeEstimate: list.resultSizeEstimate || 0, messages };
}

function gwsRun(args, account) {
  const mode = AUTH_MAP[account] || (String(account).toLowerCase().endsWith('@gmail.com') ? 'gws_oauth' : 'gws_sa');
  const env = { ...process.env };
  if (mode === 'gws_sa') {
    env.GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE = serviceAccountKeyPath(account);
    env.GOOGLE_WORKSPACE_PROJECT_ID = GWS_PROJECT;
    env.GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND = GWS_KEYRING_BACKEND;
  } else {
    delete env.GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE;
    env.GOOGLE_WORKSPACE_CLI_CONFIG_DIR = gwsConfigDir(account);
    env.GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND = GWS_KEYRING_BACKEND;
  }
  return execFileSync(GWS, args, { encoding: 'utf8', env, stdio: ['ignore', 'pipe', 'pipe'] });
}

async function listMessagesGws(account) {
  const list = parseJsonOutput(gwsRun([
    'gmail', 'users', 'messages', 'list',
    '--params', JSON.stringify({ userId: 'me', maxResults: MAX_RESULTS, q: QUERY }),
    '--format', 'json',
  ], account));
  const messages = [];
  for (const row of (list.messages || []).slice(0, MAX_RESULTS)) {
    const msg = parseJsonOutput(gwsRun([
      'gmail', 'users', 'messages', 'get',
      '--params', JSON.stringify({ userId: 'me', id: row.id, format: 'full' }),
      '--format', 'json',
    ], account));
    messages.push(msg);
  }
  return { resultSizeEstimate: list.resultSizeEstimate || 0, messages };
}

function decodePartBody(part) {
  const data = part && part.body && part.body.data;
  if (!data) return '';
  try {
    return Buffer.from(String(data).replace(/-/g, '+').replace(/_/g, '/'), 'base64').toString('utf8');
  } catch {
    return '';
  }
}

function collectMessageText(payload) {
  if (!payload) return '';
  const chunks = [decodePartBody(payload)];
  for (const part of (payload.parts || [])) chunks.push(collectMessageText(part));
  return chunks.filter(Boolean).join('\n');
}

function headerValue(msg, name) {
  for (const h of (((msg.payload || {}).headers) || [])) {
    if (String(h.name || '').toLowerCase() === name.toLowerCase()) return h.value || '';
  }
  return '';
}

function normalizeText(text) {
  return String(text || '').replace(/\s+/g, ' ').trim();
}

function sanitizeOtp(value) {
  const otp = String(value || '').replace(/\D/g, '');
  return otp.length >= 4 && otp.length <= 8 ? otp : '';
}

function extractOtp(text) {
  const normalized = normalizeText(text);
  const patterns = [
    /(?:verification|security|one[-\s]?time|otp|pin|code)[^\d]{0,60}(\d[\d\s-]{3,12}\d?)/ig,
    /(\d[\d\s-]{3,12}\d?)[^\d]{0,60}(?:verification|security|one[-\s]?time|otp|pin|code)/ig,
  ];
  for (const pattern of patterns) {
    for (const match of normalized.matchAll(pattern)) {
      const otp = sanitizeOtp(match[1]);
      if (otp) return otp;
    }
  }
  if (/(citadel|loansphere|bkiconnect)/i.test(normalized)) {
    for (const match of normalized.matchAll(/\b(\d{6})\b/g)) {
      const otp = sanitizeOtp(match[1]);
      if (otp) return otp;
    }
  }
  return '';
}

function maskDigits(text) {
  return String(text || '').replace(/\d/g, 'x').slice(0, 200);
}

function isCitadelMessage(msg) {
  const text = [
    headerValue(msg, 'From'),
    headerValue(msg, 'Subject'),
    msg.snippet || '',
    collectMessageText(msg.payload || {}),
  ].join('\n');
  return /(support@mycitadelservicing\.com|mycitadelservicing|citadel|loansphere|bkiconnect)/i.test(text);
}

function writeReport(report) {
  fs.mkdirSync(path.dirname(REPORT_PATH), { recursive: true });
  fs.writeFileSync(REPORT_PATH, JSON.stringify(report, null, 2));
}

function writeOtp(otp) {
  fs.mkdirSync(path.dirname(OTP_FILE), { recursive: true });
  fs.writeFileSync(OTP_FILE, `${otp}\n`, { mode: 0o600 });
  try { fs.chmodSync(OTP_FILE, 0o600); } catch {}
}

async function scanAccount(account) {
  const mode = AUTH_MAP[account] || (String(account).toLowerCase().endsWith('@gmail.com') ? 'gws_oauth' : 'gws_sa');
  const result = mode === 'gws_sa' ? await listMessagesSa(account) : await listMessagesGws(account);
  for (const msg of result.messages || []) {
    const internalDate = Number(msg.internalDate || 0);
    if (AFTER_EPOCH_MS && internalDate && internalDate < AFTER_EPOCH_MS) continue;
    if (!isCitadelMessage(msg)) continue;
    const text = [
      headerValue(msg, 'Subject'),
      msg.snippet || '',
      collectMessageText(msg.payload || {}),
    ].join('\n');
    const otp = extractOtp(text);
    if (otp) {
      return {
        account,
        mode,
        resultSizeEstimate: result.resultSizeEstimate,
        message_count: (result.messages || []).length,
        otp,
        selected_message: {
          id: msg.id || null,
          thread_id: msg.threadId || null,
          from: maskDigits(headerValue(msg, 'From')),
          subject: maskDigits(headerValue(msg, 'Subject')),
          date: headerValue(msg, 'Date') || null,
        },
      };
    }
  }
  return {
    account,
    mode,
    resultSizeEstimate: result.resultSizeEstimate,
    message_count: (result.messages || []).length,
  };
}

async function main() {
  const report = {
    job: 'citadel-email-otp-fetch',
    created_at: new Date().toISOString(),
    query: QUERY,
    after_epoch_ms: AFTER_EPOCH_MS || null,
    accounts_checked: [],
    otp_written: false,
    otp_file: OTP_FILE,
  };
  for (const account of ACCOUNTS) {
    try {
      const result = await scanAccount(account);
      report.accounts_checked.push({
        account,
        mode: result.mode,
        resultSizeEstimate: result.resultSizeEstimate,
        message_count: result.message_count,
        selected_message: result.selected_message || null,
        status: result.otp ? 'otp_found' : 'not_found',
      });
      if (result.otp) {
        writeOtp(result.otp);
        report.status = 'ok';
        report.otp_written = true;
        report.selected_account = account;
        writeReport(report);
        console.log(JSON.stringify({ status: 'ok', otp_written: true, report: REPORT_PATH }));
        return;
      }
    } catch (e) {
      report.accounts_checked.push({
        account,
        mode: AUTH_MAP[account] || null,
        status: 'error',
        error: String(e && e.message || e).slice(0, 300),
      });
    }
  }
  report.status = 'not_found';
  writeReport(report);
  console.log(JSON.stringify({ status: 'not_found', otp_written: false, report: REPORT_PATH }));
  process.exit(1);
}

main().catch(e => {
  const report = {
    job: 'citadel-email-otp-fetch',
    created_at: new Date().toISOString(),
    status: 'error',
    error: String(e && e.message || e).slice(0, 300),
    otp_written: false,
    otp_file: OTP_FILE,
  };
  writeReport(report);
  console.log(JSON.stringify({ status: 'error', otp_written: false, report: REPORT_PATH }));
  process.exit(2);
});
