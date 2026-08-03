#!/usr/bin/env node
const fs = require('fs');
const path = require('path');

const OPENCLAW_ROOT = process.env.OPENCLAW_ROOT || path.join(process.env.HOME || '', '.openclaw');
const ROOT = process.env.WORKSPACE_ROOT || path.join(OPENCLAW_ROOT, 'workspace');
const REPORTS = path.join(ROOT, 'reports');
const VERSION_URL = process.env.CITADEL_CDP_VERSION_URL || process.env.BASELANE_CDP_VERSION_URL || 'http://127.0.0.1:9222/json/version';
const API_BASE = 'https://api.bkiconnect.com';
const TARGET_ID = (process.env.CITADEL_TAB_SCAN_TARGET_ID || '').trim();
const SCAN_LIMIT = positiveInt(process.env.CITADEL_TAB_SCAN_LIMIT, 8);
const FETCH_TIMEOUT_MS = positiveInt(process.env.CITADEL_TAB_SCAN_FETCH_TIMEOUT_MS, 3000);
const reportPath = process.env.CITADEL_AUTH_PREFLIGHT_REPORT || path.join(REPORTS, 'citadel_auth_preflight_report.json');

function positiveInt(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.trunc(parsed) : fallback;
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
      const result = {
        bearerToken: '',
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
        result.localStorageKeyCount += 1;
        walk(localStorage.getItem(key), 'localStorage.' + key, 0);
      }
      for (let i = 0; i < sessionStorage.length; i++) {
        const key = sessionStorage.key(i);
        result.sessionStorageKeyCount += 1;
        walk(sessionStorage.getItem(key), 'sessionStorage.' + key, 0);
      }
      for (const key of Object.keys(window)) {
        if (/token|auth|bearer|source/i.test(key)) walk(window[key], 'window.' + key, 0);
      }
      return result;
    }
  `;
}

async function main() {
  fs.mkdirSync(REPORTS, { recursive: true });
  const report = {
    job: 'citadel-auth-preflight',
    started_at: new Date().toISOString(),
    status: 'unknown',
    cdp_version_url: VERSION_URL,
    scan_limit: SCAN_LIMIT,
    scan_fetch_timeout_ms: FETCH_TIMEOUT_MS,
    scan_target_id_requested: !!TARGET_ID,
    scan_target_id_found: null,
    candidate_count: 0,
    route_counts: {},
    login_tab_count: 0,
    non_login_tab_count: 0,
    scanned_count: 0,
    authenticated_found: false,
    statuses: [],
    errors: [],
  };

  let ws = null;
  try {
    const version = await (await fetch(VERSION_URL)).json();
    report.browser = version.Browser || null;
    ws = new WebSocket(version.webSocketDebuggerUrl);
    let id = 0;
    const pending = new Map();
    function send(method, params = {}, sessionId) {
      const msg = { id: ++id, method, params };
      if (sessionId) msg.sessionId = sessionId;
      ws.send(JSON.stringify(msg));
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          pending.delete(msg.id);
          reject(new Error(`CDP command timed out: ${method}`));
        }, 15000);
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

    const targets = await send('Target.getTargets');
    const candidates = prioritize((targets.targetInfos || []).filter(t => t.type === 'page' && t.url && t.url.includes('bkiconnect.com')));
    report.candidate_count = candidates.length;
    report.route_counts = routeCounts(candidates);
    report.login_tab_count = candidates.filter(t => String(t.url || '').includes('#/login')).length;
    report.non_login_tab_count = Math.max(0, candidates.length - report.login_tab_count);
    report.scan_target_id_found = TARGET_ID ? candidates.some(t => t.targetId === TARGET_ID) : null;
    for (const candidate of candidates.slice(0, SCAN_LIMIT)) {
      report.scanned_count += 1;
      let sessionId = '';
      try {
        const attached = await send('Target.attachToTarget', { targetId: candidate.targetId, flatten: true });
        sessionId = attached.sessionId;
        await send('Runtime.enable', {}, sessionId);
        const scan = await send('Runtime.evaluate', {
          awaitPromise: true,
          returnByValue: true,
          expression: `
            (async () => {
              try {
                ${storageAuthHelperSource()}
                const auth = __citadelStorageAuth();
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), ${FETCH_TIMEOUT_MS});
                const headers = { accept: 'application/json', 'x-correlation-id': String(crypto.randomUUID ? crypto.randomUUID() : Date.now()) };
                if (auth.mobileSourceId) headers['x-mobile-source-id'] = auth.mobileSourceId;
                if (auth.bearerToken) headers.authorization = 'Bearer ' + auth.bearerToken;
                const resp = await fetch('${API_BASE}/sd/loan-info/v2/my/loans-summary', {
                  credentials: 'include',
                  headers,
                  signal: controller.signal
                });
                clearTimeout(timer);
                return {
                  ok: resp.ok,
                  status: resp.status,
                  url: location.href,
                  title: document.title,
                  state: location.hash || location.pathname,
                  browser_bearer_token_available: !!auth.bearerToken,
                  browser_mobile_source_id_available: !!auth.mobileSourceId,
                  token_candidate_count: auth.tokenCandidateCount
                };
              } catch (e) {
                return {
                  ok: false,
                  error: e.message,
                  url: location.href,
                  title: document.title,
                  state: location.hash || location.pathname
                };
              }
            })()
          `,
        }, sessionId);
        const value = scan && scan.result && scan.result.value || {};
        const status = {
          target_id: candidate.targetId,
          url: candidate.url,
          title: candidate.title || null,
          ok: !!value.ok,
          status: value.status || null,
          state: value.state || null,
          browser_bearer_token_available: !!value.browser_bearer_token_available,
          browser_mobile_source_id_available: !!value.browser_mobile_source_id_available,
          token_candidate_count: value.token_candidate_count || 0,
          error: value.error ? String(value.error).slice(0, 160) : null,
        };
        report.statuses.push(status);
        if (value.ok) {
          report.authenticated_found = true;
          report.authenticated_target_id = candidate.targetId;
          report.status = 'ok';
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
        if (sessionId) await send('Target.detachFromTarget', { sessionId }).catch(() => null);
      }
    }
    if (!report.authenticated_found) {
      report.status = 'review';
      report.manual_auth_required = true;
      report.reason = report.candidate_count ? 'no_authenticated_citadel_tab' : 'no_citadel_tab';
    }
  } catch (error) {
    report.status = 'error';
    report.errors.push(String(error && error.message || error).slice(0, 300));
  } finally {
    report.ended_at = new Date().toISOString();
    fs.writeFileSync(reportPath, JSON.stringify(report, null, 2));
    if (ws) ws.close();
  }
  console.log(`[citadel-preflight] ${report.status} authenticated=${report.authenticated_found} scanned=${report.scanned_count}/${report.candidate_count}`);
  process.exit(report.status === 'ok' ? 0 : 1);
}

main();
