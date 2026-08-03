#!/usr/bin/env node
const fs = require('fs');

// Use ws package for custom header support (needed for Docker→Windows CDP)
let WebSocketImpl;
try { WebSocketImpl = require('ws'); }
catch (_e) { WebSocketImpl = WebSocket; }

const versionUrl = process.env.BASELANE_CDP_VERSION_URL || 'http://127.0.0.1:19222/json/version';
const targetUrl = process.env.BASELANE_GQL_TARGET_URL || 'https://app.baselane.com/transactions';
const timeoutMs = Number(process.env.BASELANE_GQL_TIMEOUT_MS || 60000);
const commandTimeoutMs = Number(process.env.BASELANE_GQL_COMMAND_TIMEOUT_MS || 15000);
const runtimeFailureLimit = Number(process.env.BASELANE_GQL_RUNTIME_FAILURE_LIMIT || 4);
const directTabEnabled = process.env.BASELANE_GQL_DIRECT_TAB !== '0';
const playwrightFallbackEnabled = process.env.BASELANE_GQL_PLAYWRIGHT_FALLBACK === '1';
const targetLimit = Math.max(1, Number(process.env.BASELANE_GQL_TARGET_LIMIT || 2));
const requestedTargetId = String(process.env.BASELANE_GQL_TARGET_ID || '').trim();
const createTargetEnabled = process.env.BASELANE_GQL_CREATE_TARGET === '1';
const initialReloadMs = Math.max(250, Number(process.env.BASELANE_GQL_INITIAL_RELOAD_MS || 1000));
const sessionCacheTtlMs = Math.max(1000, Number(process.env.BASELANE_GQL_SESSION_CACHE_TTL_MS || 600000));
const sessionCachePath = process.env.BASELANE_GQL_SESSION_CACHE_PATH
  || `/tmp/baselane-graphql-session-${typeof process.getuid === 'function' ? process.getuid() : 'user'}-${requestedTargetId || 'shared'}.json`;
const inputPath = process.argv[2];
if (!inputPath) {
  console.error('usage: baselane_graphql_via_cdp.js <input.json>');
  process.exit(2);
}
const input = JSON.parse(fs.readFileSync(inputPath, 'utf8'));
const requestHeaders = {};
if (input && input.requestHeaders && typeof input.requestHeaders === 'object') {
  const idempotencyKey = input.requestHeaders['x-idempotency-key'];
  if (typeof idempotencyKey === 'string' && idempotencyKey.trim()) {
    requestHeaders['x-idempotency-key'] = idempotencyKey.trim();
  }
}
const graphqlInputs = Array.isArray(input && input.batchOperations)
  ? input.batchOperations.map(operation => ({ ...operation }))
  : [{ ...input }];
for (const operation of graphqlInputs) delete operation.requestHeaders;
const graphqlInput = graphqlInputs[0];
if (!graphqlInput) {
  console.error('input must contain a GraphQL operation or non-empty batchOperations');
  process.exit(2);
}

async function fetchCdpVersion() {
  const _cdpUrl = new URL(versionUrl);
  const _cdpIsRemote = _cdpUrl.hostname !== 'localhost' && _cdpUrl.hostname !== '127.0.0.1' && _cdpUrl.hostname !== '[::1]';
  // Node's fetch doesn't reliably send Host header overrides; use http.get for remote CDP
  if (_cdpIsRemote) {
    const { get } = require('http');
    return await new Promise((resolve, reject) => {
      get({ hostname: _cdpUrl.hostname, port: _cdpUrl.port || 80, path: _cdpUrl.pathname, headers: { Host: 'localhost' } }, r => {
        let d = '';
        r.on('data', c => d += c);
        r.on('end', () => { try { resolve(JSON.parse(d)); } catch (e) { reject(e); } });
      }).on('error', reject);
    });
  }
  const versionController = new AbortController();
  const versionTimer = setTimeout(() => versionController.abort(), commandTimeoutMs);
  try {
    const versionResponse = await fetch(versionUrl, { signal: versionController.signal });
    return await versionResponse.json();
  } finally {
    clearTimeout(versionTimer);
  }
}

function rewriteWsUrl(wsUrl) {
  // Rewrite ws://localhost or ws://127.0.0.1 URLs to use the CDP host from BASELANE_CDP_VERSION_URL
  // so WebSocket connections work from inside Docker containers (host.docker.internal)
  if (process.env.BASELANE_CDP_VERSION_URL) {
    try {
      const cdpUrl = new URL(process.env.BASELANE_CDP_VERSION_URL);
      const cdpHost = cdpUrl.hostname;
      const cdpPort = cdpUrl.port || (cdpUrl.protocol === 'https:' ? '443' : '80');
      return wsUrl.replace(/ws:\/\/(localhost|127\.0\.0\.1|\[::1\])(:\d+)?\//, `ws://${cdpHost}:${cdpPort}/`);
    } catch (_e) { /* leave wsUrl unchanged if URL parsing fails */ }
  }
  return wsUrl;
}

function makeWsOpts(wsUrl) {
  const wsOpts = {};
  try {
    const _wu = new URL(wsUrl);
    if (_wu.hostname !== 'localhost' && _wu.hostname !== '127.0.0.1' && _wu.hostname !== '[::1]') {
      wsOpts.headers = { Host: 'localhost' };
    }
  } catch (_e) { /* ignore */ }
  return wsOpts;
}

function isBaselaneUrl(url) {
  return String(url || '').startsWith('https://app.baselane.com/');
}

function isBaselaneLoginUrl(url) {
  const value = String(url || '');
  return value.includes('/login') || value.includes('/session-expired') || value.includes('/error');
}

function isUsableBaselanePage(targetInfo) {
  return targetInfo && targetInfo.type === 'page' && isBaselaneUrl(targetInfo.url) && !isBaselaneLoginUrl(targetInfo.url);
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function errorMessage(err) {
  if (!err) return '';
  if (err.message) return String(err.message);
  try {
    return JSON.stringify(err);
  } catch (_jsonErr) {
    return String(err);
  }
}

function withTimeout(promise, ms, label) {
  let timer = null;
  return new Promise((resolve, reject) => {
    timer = setTimeout(() => reject(new Error(`timeout: ${label}`)), ms);
    promise.then(
      value => {
        clearTimeout(timer);
        resolve(value);
      },
      err => {
        clearTimeout(timer);
        reject(err);
      }
    );
  });
}

function cdpBaseUrl() {
  const url = new URL(versionUrl);
  if (url.pathname.endsWith('/json/version')) {
    url.pathname = '';
  }
  url.search = '';
  url.hash = '';
  return url.toString().replace(/\/$/, '');
}

function tabSortKey(tab) {
  const url = String(tab.url || '');
  if (url.includes('/transactions')) return 0;
  if (isUsableBaselanePage(tab)) return 1;
  return 2;
}

function capturedGraphqlHeadersFrom(extraInfoHeaders) {
  const h = extraInfoHeaders || {};
  const appCheck = h['x-firebase-appcheck'] || h['X-Firebase-AppCheck'] || '';
  const cookie = h.cookie || h.Cookie || '';
  if (!appCheck || !cookie) return null;
  const headers = {
    accept: h.accept || '*/*',
    'content-type': h['content-type'] || h['Content-Type'] || 'application/json',
    origin: h.origin || 'https://app.baselane.com',
    referer: h.referer || 'https://app.baselane.com/',
    'user-agent': h['user-agent'] || h['User-Agent'] || '',
    'x-firebase-appcheck': appCheck,
    cookie,
  };
  const gmpid = h['x-firebase-gmpid'] || h['X-Firebase-GMPID'];
  if (gmpid) headers['x-firebase-gmpid'] = gmpid;
  return headers;
}

function readFreshSessionCache() {
  try {
    const stat = fs.statSync(sessionCachePath);
    if (Date.now() - stat.mtimeMs > sessionCacheTtlMs) return null;
    const payload = JSON.parse(fs.readFileSync(sessionCachePath, 'utf8'));
    const headers = payload && payload.headers;
    if (!headers || !headers['x-firebase-appcheck'] || !headers.cookie) return null;
    return headers;
  } catch (_err) {
    return null;
  }
}

function writeSessionCache(headers) {
  if (!headers || !headers['x-firebase-appcheck'] || !headers.cookie) return;
  fs.writeFileSync(
    sessionCachePath,
    JSON.stringify({ capturedAt: new Date().toISOString(), headers }),
    { mode: 0o600 }
  );
  try { fs.chmodSync(sessionCachePath, 0o600); } catch (_err) {}
}

async function fetchGraphqlWithCapturedHeaders(headers, label) {
  if (!headers || !headers['x-firebase-appcheck'] || !headers.cookie) {
    throw new Error(`${label}: missing captured AppCheck or cookie headers`);
  }
  console.error(`[CDP] Executing GraphQL via captured ${label} headers`);
  const results = [];
  for (const operation of graphqlInputs) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), commandTimeoutMs);
    let response;
    try {
      response = await fetch('https://orchestration.baselane.com/graphql', {
        method: 'POST',
        headers: { ...headers, ...requestHeaders },
        body: JSON.stringify(operation),
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timer);
    }
    const text = await response.text();
    if (response.status !== 200) {
      throw new Error(`${label.toUpperCase()}_GRAPHQL_STATUS_${response.status}: ${String(text || '').slice(0, 500)}`);
    }
    const parsed = JSON.parse(text);
    results.push(parsed);
    if (parsed && parsed.errors) {
      throw new Error(`${label.toUpperCase()}_GRAPHQL_ERRORS: ${JSON.stringify(parsed.errors).slice(0, 1000)}`);
    }
  }
  await writeStdoutLine(
    results.length === 1 ? JSON.stringify(results[0]) : JSON.stringify({ batchResults: results })
  );
  return true;
}

function writeStdoutLine(text) {
  return new Promise((resolve, reject) => {
    process.stdout.write(`${text}\n`, err => {
      if (err) reject(err); else resolve();
    });
  });
}

function parseEvaluateJsonResult(result, label) {
  if (result && result.exceptionDetails) {
    throw new Error(`${label}_RUNTIME_EXCEPTION: ${JSON.stringify(result.exceptionDetails).slice(0, 800)}`);
  }
  const rawOuter = result && result.result && result.result.value;
  if (typeof rawOuter !== 'string') {
    throw new Error(`${label}_MISSING_RUNTIME_VALUE: ${JSON.stringify(result || {}).slice(0, 800)}`);
  }
  return JSON.parse(rawOuter);
}

async function fetchCdpTabs() {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), commandTimeoutMs);
  try {
    const response = await fetch(`${cdpBaseUrl()}/json/list`, { signal: controller.signal });
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

async function directPageGraphqlAttempt(tab) {
  if (!tab || !tab.webSocketDebuggerUrl) throw new Error('missing direct page websocket url');
    const wsUrl = rewriteWsUrl(tab.webSocketDebuggerUrl);
    const wsOpts = makeWsOpts(wsUrl);
    const ws = new WebSocketImpl(wsUrl, wsOpts);
  let id = 0;
  const pending = new Map();
  let lastAppCheck = null;
  let lastGraphqlHeaders = null;
  let runtimeFailureCount = 0;

  function send(method, params = {}, timeoutOverrideMs = null) {
    const msgId = ++id;
    ws.send(JSON.stringify({ id: msgId, method, params }));
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(msgId);
        reject(new Error(`timeout: ${method}`));
      }, timeoutOverrideMs || commandTimeoutMs);
      pending.set(msgId, {
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

  function noteRuntimeFailure(label, err) {
    runtimeFailureCount += 1;
    const message = errorMessage(err);
    console.error(`[CDP direct] ${label} failed (${runtimeFailureCount}/${runtimeFailureLimit}): ${message}`);
    if (runtimeFailureCount >= runtimeFailureLimit) {
      throw new Error(`DIRECT_RUNTIME_UNRESPONSIVE: ${label}: ${message}`);
    }
  }

  ws.onmessage = ev => {
    const msg = JSON.parse(ev.data);
    if (msg.id) {
      const p = pending.get(msg.id);
      if (p) {
        pending.delete(msg.id);
        if (msg.error) p.reject(new Error(errorMessage(msg.error))); else p.resolve(msg.result);
      }
      return;
    }
    if (msg.method === 'Network.requestWillBeSentExtraInfo') {
      const h = msg.params.headers || {};
      const authority = h[':authority'] || h.host || h.Host || '';
      if (String(authority).includes('orchestration.baselane.com')) {
        const v = h['x-firebase-appcheck'] || h['X-Firebase-AppCheck'];
        if (v) lastAppCheck = v;
        const captured = capturedGraphqlHeadersFrom(h);
        if (captured) lastGraphqlHeaders = captured;
      }
    }
  };

  // Handle messages from both ws package (Buffer) and built-in WebSocket (ev.data)
  function handleWsMessage(rawData) {
    const text = typeof rawData === 'string' ? rawData : (rawData && rawData.toString ? rawData.toString() : String(rawData));
    const msg = JSON.parse(text);
    if (msg.id) {
      const p = pending.get(msg.id);
      if (p) {
        pending.delete(msg.id);
        if (msg.error) p.reject(new Error(errorMessage(msg.error))); else p.resolve(msg.result);
      }
      return;
    }
    if (msg.method === 'Network.requestWillBeSentExtraInfo') {
      const h = msg.params.headers || {};
      const authority = h[':authority'] || h['host'] || h['Host'] || '';
      if (String(authority).includes('orchestration.baselane.com')) {
        const v = h['x-firebase-appcheck'] || h['X-Firebase-AppCheck'];
        if (v) lastAppCheck = v;
        const captured = capturedGraphqlHeadersFrom(h);
        if (captured) lastGraphqlHeaders = captured;
      }
    }
  }

  // Wire message handler: ws package uses 'message' event with Buffer; built-in uses onmessage with ev.data
  if (typeof ws.on === 'function') {
    ws.on('message', handleWsMessage);
  } else {
    ws.onmessage = (ev) => handleWsMessage(ev.data);
  }

  // Wait for connection: ws package uses 'open' event; built-in uses onopen
  await withTimeout(new Promise((resolve, reject) => {
    if (typeof ws.on === 'function') {
      ws.on('open', () => { resolve(); });
      ws.on('error', (err) => { reject(err); });
    } else {
      ws.onopen = resolve;
      ws.onerror = reject;
    }
  }), commandTimeoutMs, 'direct page websocket open');
  try {
    await send('Network.enable', {}, 5000);
    await send('Runtime.enable', {}, 5000);
    await send('Page.enable', {}, 5000).catch(() => {});

    async function evaluate(expression, awaitPromise = true, timeoutOverrideMs = null) {
      return await send('Runtime.evaluate', { expression, awaitPromise, returnByValue: true }, timeoutOverrideMs);
    }

    async function currentHref() {
      const result = await evaluate('location.href', true, 5000).catch(err => {
        noteRuntimeFailure('location.href check', err);
        return null;
      });
      const href = result && result.result && result.result.value || '';
      if (href) runtimeFailureCount = 0;
      return href;
    }

    const initialHref = await currentHref();
    if (isBaselaneLoginUrl(initialHref)) {
      throw new Error(`DIRECT_AUTH_REQUIRED: ${initialHref}`);
    }
    if (!String(initialHref || '').includes('/transactions')) {
      await send('Page.navigate', { url: targetUrl }, 5000).catch(err => {
      console.error('[CDP direct] transactions navigation did not confirm quickly: ' + errorMessage(err));
      });
      await sleep(2000);
      const afterNav = await currentHref();
      if (isBaselaneLoginUrl(afterNav)) throw new Error(`DIRECT_AUTH_REQUIRED: ${afterNav}`);
    }

    async function triggerGraphqlTraffic(label) {
      console.error(`[CDP direct] Triggering read-only GraphQL traffic for appcheck capture: ${label}`);
      await evaluate(`
        (() => {
          try {
            const controller = new AbortController();
            setTimeout(() => controller.abort(), 8000);
            fetch('https://orchestration.baselane.com/graphql', {
              method: 'POST',
              credentials: 'include',
              signal: controller.signal,
              headers: {'accept': '*/*', 'content-type': 'application/json'},
              body: JSON.stringify({
                operationName: 'PropertyList',
                variables: {},
                query: 'query PropertyList { property { id } }'
              })
            }).catch(() => {});
          } catch (e) {}
          return true;
        })()
      `, true, 10000).catch(err => {
        noteRuntimeFailure('read-only GraphQL trigger', err);
      });
    }

    await triggerGraphqlTraffic('direct initial');
    const start = Date.now();
    while (!lastAppCheck && (Date.now() - start) < timeoutMs) {
      await sleep(250);
      const elapsed = Date.now() - start;
      if (!lastAppCheck && elapsed > initialReloadMs && elapsed < initialReloadMs + 500) {
        await send('Page.reload', { ignoreCache: true }, 5000).catch(err => {
          console.error('[CDP direct] reload did not confirm quickly: ' + errorMessage(err));
        });
        await sleep(2000);
        await triggerGraphqlTraffic('direct after reload');
      }
      if (!lastAppCheck && elapsed > 20000 && elapsed < 20500) {
        await triggerGraphqlTraffic('direct second explicit trigger');
      }
    }
    if (!lastAppCheck) throw new Error(`DIRECT_APPCHECK_TIMEOUT: ${await currentHref() || tab.url || 'unknown-url'}`);
    if (lastGraphqlHeaders) {
      writeSessionCache(lastGraphqlHeaders);
      await fetchGraphqlWithCapturedHeaders(lastGraphqlHeaders, 'direct page');
      return true;
    }

    const expression = `
      (async () => {
        const payloads = ${JSON.stringify(graphqlInputs)};
        const results = [];
        for (const payload of payloads) {
          const resp = await fetch('https://orchestration.baselane.com/graphql', {
            method: 'POST',
            credentials: 'include',
            headers: {
              'accept': '*/*',
              'content-type': 'application/json',
              'x-firebase-appcheck': ${JSON.stringify(lastAppCheck)},
              ...${JSON.stringify(requestHeaders)}
            },
            body: JSON.stringify(payload)
          });
          const text = await resp.text();
          if (resp.status !== 200) return JSON.stringify({ status: resp.status, text });
          const parsed = JSON.parse(text);
          results.push(parsed);
          if (parsed && parsed.errors) return JSON.stringify({ status: 200, text: JSON.stringify(parsed) });
        }
        return JSON.stringify({
          status: 200,
          text: JSON.stringify(results.length === 1 ? results[0] : {batchResults: results})
        });
      })()
    `;
    const result = await evaluate(expression, true, commandTimeoutMs);
    const outer = parseEvaluateJsonResult(result, 'DIRECT_GRAPHQL_EVALUATE');
    if (outer.status !== 200) throw new Error(`DIRECT_GRAPHQL_STATUS_${outer.status}: ${String(outer.text || '').slice(0, 500)}`);
    await writeStdoutLine(typeof outer.text === 'string' ? outer.text : JSON.stringify(outer));
    return true;
  } finally {
    try { ws.close(); } catch (_err) {}
  }
}

async function tryDirectPageGraphql() {
  const tabs = await fetchCdpTabs();
  const candidates = (Array.isArray(tabs) ? tabs : [])
    .filter(t => t.type === 'page' && isBaselaneUrl(t.url) && !isBaselaneLoginUrl(t.url) && t.webSocketDebuggerUrl)
    .filter(t => !requestedTargetId || t.id === requestedTargetId)
    .sort((a, b) => tabSortKey(a) - tabSortKey(b))
    .slice(0, targetLimit);
  if (!candidates.length) throw new Error(requestedTargetId
    ? `requested Baselane page target is unavailable: ${requestedTargetId}`
    : 'no direct authenticated-looking Baselane page targets');
  const errors = [];
  for (const tab of candidates) {
    try {
      console.error('[CDP direct] Trying Baselane page target: ' + String(tab.url || '').substring(0, 80));
      await directPageGraphqlAttempt(tab);
      return true;
    } catch (err) {
      const message = errorMessage(err);
      errors.push(`${tab.id || 'unknown'} ${message}`);
      console.error('[CDP direct] Baselane page target failed: ' + message);
    }
  }
  throw new Error('direct page GraphQL failed: ' + errors.join(' | '));
}

async function playwrightPageGraphqlAttempt() {
  // Raw CDP occasionally accepts the websocket but stops acknowledging domain
  // commands after a browser restart. Playwright's existing visible-browser
  // connection remains healthy in that state, so use it as a non-headless,
  // same-tab fallback. No new browser or tab is created.
  // The canonical OpenClaw workspace provides playwright-core at the
  // workspace root; Node's normal parent-directory resolution finds it from
  // this repo without bundling a second browser runtime here.
  const { chromium } = require('playwright-core');
  const browser = await chromium.connectOverCDP(cdpBaseUrl());
  const pages = browser.contexts().flatMap(context => context.pages());
  const page = pages
    .filter(candidate => isBaselaneUrl(candidate.url()) && !isBaselaneLoginUrl(candidate.url()))
    .sort((a, b) => tabSortKey({ url: a.url() }) - tabSortKey({ url: b.url() }))[0];
  if (!page) throw new Error('no authenticated visible Baselane page available to Playwright');

  let lastAppCheck = '';
  let lastGraphqlHeaders = null;
  const capture = async request => {
    if (!request.url().includes('orchestration.baselane.com/graphql')) return;
    try {
      const headers = await request.allHeaders();
      const value = headers['x-firebase-appcheck'] || '';
      if (value) {
        lastAppCheck = value;
        lastGraphqlHeaders = capturedGraphqlHeadersFrom(headers);
        if (lastGraphqlHeaders) writeSessionCache(lastGraphqlHeaders);
      }
    } catch (_err) {
      // A request may finish before allHeaders resolves; the next request will
      // provide another opportunity during the reload below.
    }
  };
  page.on('request', capture);
  try {
    if (!page.url().includes('/transactions')) {
      await page.goto(targetUrl, { waitUntil: 'domcontentloaded', timeout: timeoutMs });
    } else {
      await page.reload({ waitUntil: 'domcontentloaded', timeout: timeoutMs });
    }
    const started = Date.now();
    while (!lastAppCheck && Date.now() - started < timeoutMs) {
      await page.waitForTimeout(250);
    }
    if (!lastAppCheck) throw new Error(`Playwright timed out waiting for AppCheck at ${page.url()}`);

    const outer = await page.evaluate(async ({ payloads, appCheck, extraHeaders }) => {
      const results = [];
      for (const payload of payloads) {
        const response = await fetch('https://orchestration.baselane.com/graphql', {
          method: 'POST',
          credentials: 'include',
          headers: {
            accept: '*/*',
            'content-type': 'application/json',
            'x-firebase-appcheck': appCheck,
            ...extraHeaders,
          },
          body: JSON.stringify(payload),
        });
        const text = await response.text();
        if (response.status !== 200) return { status: response.status, text };
        const parsed = JSON.parse(text);
        results.push(parsed);
        if (parsed && parsed.errors) return { status: 200, text: JSON.stringify(parsed) };
      }
      return {
        status: 200,
        text: JSON.stringify(results.length === 1 ? results[0] : { batchResults: results }),
      };
    }, { payloads: graphqlInputs, appCheck: lastAppCheck, extraHeaders: requestHeaders });
    if (outer.status !== 200) {
      throw new Error(`PLAYWRIGHT_GRAPHQL_STATUS_${outer.status}: ${String(outer.text || '').slice(0, 500)}`);
    }
    const parsed = JSON.parse(outer.text);
    if (parsed && parsed.errors) {
      throw new Error(`PLAYWRIGHT_GRAPHQL_ERRORS: ${JSON.stringify(parsed.errors).slice(0, 1000)}`);
    }
    await writeStdoutLine(outer.text);
    return true;
  } finally {
    page.off('request', capture);
  }
}

async function main() {
  if (directTabEnabled) {
    const cachedHeaders = readFreshSessionCache();
    if (cachedHeaders) {
      try {
        await fetchGraphqlWithCapturedHeaders(cachedHeaders, 'session cache');
        return;
      } catch (err) {
        console.error('[Session cache] Refreshing visible-page credentials: ' + errorMessage(err));
      }
    }
    try {
      await tryDirectPageGraphql();
      return;
    } catch (err) {
      console.error('[CDP direct] Raw page target failed: ' + errorMessage(err));
    }
    if (playwrightFallbackEnabled) {
      try {
        await playwrightPageGraphqlAttempt();
        return;
      } catch (err) {
        console.error('[Playwright CDP] Falling back to browser Target flow: ' + errorMessage(err));
      }
    }
  }

  const versionController = new AbortController();
  const versionTimer = setTimeout(() => versionController.abort(), commandTimeoutMs);
  let versionResponse;
  try {
    versionResponse = await fetch(versionUrl, { signal: versionController.signal });
  } finally {
    clearTimeout(versionTimer);
  }
  const version = await fetchCdpVersion();
  const wsUrl = rewriteWsUrl(version.webSocketDebuggerUrl);
  const wsOpts = makeWsOpts(wsUrl);
  const ws = new WebSocketImpl(wsUrl, wsOpts);
  let id = 0;
  const pending = new Map();
  const sessions = new Map();
  let lastAppCheck = null;
  let lastGraphqlHeaders = null;
  let runtimeFailureCount = 0;

  function noteRuntimeFailure(label, err) {
    runtimeFailureCount += 1;
    const message = errorMessage(err);
    console.error(`[CDP] ${label} failed (${runtimeFailureCount}/${runtimeFailureLimit}): ${message}`);
    if (runtimeFailureCount >= runtimeFailureLimit) {
      throw new Error(`RUNTIME_UNRESPONSIVE: ${label}: ${message}`);
    }
  }

  function send(method, params = {}, sessionId, timeoutOverrideMs = null) {
    const msgId = ++id;
    const msg = { id: msgId, method, params };
    if (sessionId) msg.sessionId = sessionId;
    ws.send(JSON.stringify(msg));
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(msgId);
        reject(new Error(`timeout: ${method}`));
      }, timeoutOverrideMs || commandTimeoutMs);
      pending.set(msgId, {
        resolve: value => {
          clearTimeout(timer);
          resolve(value);
        },
        reject: error => {
          clearTimeout(timer);
          reject(error);
        },
        method,
      });
    });
  }

  ws.onmessage = ev => {
    const msg = JSON.parse(ev.data);
    if (msg.id) {
      const p = pending.get(msg.id);
      if (p) {
        pending.delete(msg.id);
        if (msg.error) p.reject(new Error(errorMessage(msg.error))); else p.resolve(msg.result);
      }
      return;
    }
    if (msg.method === 'Target.attachedToTarget') {
      sessions.set(msg.params.targetInfo.targetId, msg.params.sessionId);
      return;
    }
    if (msg.sessionId && msg.method === 'Network.requestWillBeSentExtraInfo') {
      const h = msg.params.headers || {};
      const authority = h[':authority'] || h['host'] || h['Host'] || '';
      if (String(authority).includes('orchestration.baselane.com')) {
        const v = h['x-firebase-appcheck'] || h['X-Firebase-AppCheck'];
        if (v) lastAppCheck = v;
        const captured = capturedGraphqlHeadersFrom(h);
        if (captured) lastGraphqlHeaders = captured;
      }
    }
  };

  // Handle messages from both ws package (Buffer) and built-in WebSocket (ev.data)
  function handleWsMessageMain(rawData) {
    const text = typeof rawData === 'string' ? rawData : (rawData && rawData.toString ? rawData.toString() : String(rawData));
    const msg = JSON.parse(text);
    if (msg.id) {
      const p = pending.get(msg.id);
      if (p) {
        pending.delete(msg.id);
        if (msg.error) p.reject(new Error(errorMessage(msg.error))); else p.resolve(msg.result);
      }
      return;
    }
    if (msg.method === 'Target.attachedToTarget') {
      sessions.set(msg.params.targetInfo.targetId, msg.params.sessionId);
      return;
    }
    if (msg.sessionId && msg.method === 'Network.requestWillBeSentExtraInfo') {
      const h = msg.params.headers || {};
      const authority = h[':authority'] || h['host'] || h['Host'] || '';
      if (String(authority).includes('orchestration.baselane.com')) {
        const v = h['x-firebase-appcheck'] || h['X-Firebase-AppCheck'];
        if (v) lastAppCheck = v;
        const captured = capturedGraphqlHeadersFrom(h);
        if (captured) lastGraphqlHeaders = captured;
      }
    }
  }

  // Wire message handler: ws package uses 'message' event with Buffer; built-in uses onmessage with ev.data
  if (typeof ws.on === 'function') {
    ws.on('message', handleWsMessageMain);
  } else {
    ws.onmessage = (ev) => handleWsMessageMain(ev.data);
  }

  // Wait for connection: ws package uses 'open' event; built-in uses onopen
  await withTimeout(new Promise((resolve, reject) => {
    if (typeof ws.on === 'function') {
      ws.on('open', () => { resolve(); });
      ws.on('error', (err) => { reject(err); });
    } else {
      ws.onopen = resolve;
      ws.onerror = reject;
    }
  }), commandTimeoutMs, 'browser websocket open');
  await send('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: false, flatten: true });

  async function attachAndPrepare(targetInfo) {
    console.error('[CDP] Trying Baselane tab: ' + String(targetInfo.url || '').substring(0, 80));
    const attached = await send('Target.attachToTarget', { targetId: targetInfo.targetId, flatten: true });
    const candidateSessionId = attached.sessionId;
    if (!candidateSessionId) throw new Error('Target.attachToTarget returned no session id');
    try {
      await send('Network.enable', {}, candidateSessionId, 5000);
      await send('Runtime.enable', {}, candidateSessionId, 5000);
      await send('Page.enable', {}, candidateSessionId, 5000);
      await send('Network.setCacheDisabled', { cacheDisabled: true }, candidateSessionId).catch(() => {});
      const hrefResult = await send(
        'Runtime.evaluate',
        { expression: 'location.href', returnByValue: true },
        candidateSessionId,
        5000
      );
      return {
        targetId: targetInfo.targetId,
        sessionId: candidateSessionId,
        href: hrefResult && hrefResult.result && hrefResult.result.value || '',
      };
    } catch (err) {
      await send('Target.detachFromTarget', { sessionId: candidateSessionId }).catch(() => {});
      throw err;
    }
  }

  // Reuse existing Baselane tab (already authenticated by login-and-wait script),
  // but do not let one stale CDP target block the read-only duplicate check.
  const targets = await send('Target.getTargets', {});
  const baselanePages = (targets.targetInfos || []).filter(t => t.type === 'page' && isBaselaneUrl(t.url));
  const reusableBaselanePages = baselanePages
    .filter(isUsableBaselanePage)
    .filter(t => !requestedTargetId || t.targetId === requestedTargetId);
  const existingCandidates = reusableBaselanePages.sort((a, b) => {
    const aTx = String(a.url || '').includes('/transactions') ? 0 : 1;
    const bTx = String(b.url || '').includes('/transactions') ? 0 : 1;
    return aTx - bTx;
  }).slice(0, targetLimit);

  let targetId = null;
  let sessionId = null;
  let sessionInitialHref = '';
  for (const candidate of existingCandidates) {
    try {
      const prepared = await attachAndPrepare(candidate);
      targetId = prepared.targetId;
      sessionId = prepared.sessionId;
      sessionInitialHref = prepared.href || '';
      console.error('[CDP] Reusing prepared Baselane tab: ' + String(candidate.url || '').substring(0, 80));
      break;
    } catch (err) {
      console.error('[CDP] Baselane tab unusable, trying next: ' + errorMessage(err));
    }
  }

  if (!sessionId) {
    if (!createTargetEnabled) {
      if (baselanePages.length && !reusableBaselanePages.length) {
        console.error('[CDP] Existing Baselane tabs are login/error pages; not opening a new tab because BASELANE_GQL_CREATE_TARGET is not enabled');
      } else if (existingCandidates.length) {
        console.error('[CDP] Existing Baselane tabs were unusable; not opening a new tab because BASELANE_GQL_CREATE_TARGET is not enabled');
      } else {
        console.error('[CDP] No existing usable Baselane tab; not opening a new tab because BASELANE_GQL_CREATE_TARGET is not enabled');
      }
      throw new Error(
        'NO_USABLE_BASELANE_TARGET: no reusable authenticated Baselane tab within target limit; '
        + 'reuse an existing authenticated Baselane tab or hard-refresh/login that tab'
      );
    }
    if (baselanePages.length && !reusableBaselanePages.length) {
      console.error('[CDP] Existing Baselane tabs are login/error pages — opening a fresh transactions tab');
    } else if (existingCandidates.length) {
      console.error('[CDP] Existing Baselane tabs were unusable — opening a fresh transactions tab');
    } else {
      console.error('[CDP] No existing usable Baselane tab — creating new one');
    }
    const result = await send('Target.createTarget', { url: targetUrl });
    const targetInfo = { targetId: result.targetId, type: 'page', url: targetUrl };
    const prepared = await attachAndPrepare(targetInfo);
    targetId = prepared.targetId;
    sessionId = prepared.sessionId;
    sessionInitialHref = prepared.href || '';
  }
  if (!sessionId) throw new Error('no session attached');

  if (!String(sessionInitialHref || '').includes('/transactions')) {
    await send('Page.navigate', { url: targetUrl }, sessionId, 5000).catch(err => {
      console.error('[CDP] Initial transactions navigation did not confirm quickly: ' + errorMessage(err));
    });
  }

  async function currentHref() {
    const href = await send(
      'Runtime.evaluate',
      { expression: 'location.href', returnByValue: true },
      sessionId,
      3000
    ).catch(err => {
      noteRuntimeFailure('location.href check', err);
      return null;
    });
    if (href && href.result && href.result.value) runtimeFailureCount = 0;
    return href && href.result && href.result.value || '';
  }

  async function evaluate(expression, awaitPromise = true, timeoutOverrideMs = null) {
    return await send('Runtime.evaluate', { expression, awaitPromise, returnByValue: true }, sessionId, timeoutOverrideMs);
  }

  async function triggerGraphqlTraffic(label) {
    console.error(`[CDP] Triggering read-only GraphQL traffic for appcheck capture: ${label}`);
    await evaluate(`
      (() => {
        try {
          const controller = new AbortController();
          setTimeout(() => controller.abort(), 8000);
          fetch('https://orchestration.baselane.com/graphql', {
            method: 'POST',
            credentials: 'include',
            signal: controller.signal,
            headers: {'accept': '*/*', 'content-type': 'application/json'},
            body: JSON.stringify({
              operationName: 'PropertyList',
              variables: {},
              query: 'query PropertyList { property { id } }'
            })
          }).catch(() => {});
        } catch (e) {}
        return true;
      })()
    `, true, 10000).catch(err => {
      noteRuntimeFailure('read-only GraphQL trigger', err);
    });
  }

  const navStart = Date.now();
  while ((Date.now() - navStart) < timeoutMs) {
    const current = await currentHref();
    if (isBaselaneUrl(current) && !isBaselaneLoginUrl(current)) break;
    await new Promise(r => setTimeout(r, 250));
  }
  if (isBaselaneLoginUrl(await currentHref())) {
    console.error('[CDP] GraphQL target redirected to login — hard refreshing with cache bypass');
    await send('Network.clearBrowserCache', {}, sessionId).catch(() => {});
    await send('Page.reload', { ignoreCache: true }, sessionId, 5000).catch(err => {
      console.error('[CDP] Login hard reload did not confirm quickly: ' + errorMessage(err));
    });
    await new Promise(r => setTimeout(r, 3000));
    if (isBaselaneLoginUrl(await currentHref())) {
      await send('Page.navigate', { url: targetUrl }, sessionId, 5000).catch(err => {
        console.error('[CDP] Login redirect transactions navigation did not confirm quickly: ' + errorMessage(err));
      });
      await new Promise(r => setTimeout(r, 3000));
    }
  }
  if (isBaselaneLoginUrl(await currentHref())) {
    throw new Error('AUTH_REQUIRED: Baselane GraphQL target is login after hard refresh; refresh/reopen Baselane and rerun the read-only check');
  }

  console.error('[CDP] Hard refreshing Baselane page to capture appcheck token...');
  await send('Network.clearBrowserCache', {}, sessionId).catch(() => {});
  await send('Page.reload', { ignoreCache: true }, sessionId, 5000).catch(err => {
    console.error('[CDP] Appcheck hard reload did not confirm quickly: ' + errorMessage(err));
  });
  await sleep(2000);
  await triggerGraphqlTraffic('after hard refresh');

  const start = Date.now();
  while (!lastAppCheck && (Date.now() - start) < timeoutMs) {
    await sleep(250);
    if (!lastAppCheck && Date.now() - start > 10000 && Date.now() - start < 10500) {
      console.error('[CDP] Appcheck not seen after reload; navigating transactions again');
      await send('Page.navigate', { url: targetUrl }, sessionId, 5000).catch(err => {
        console.error('[CDP] Appcheck transactions navigation did not confirm quickly: ' + errorMessage(err));
      });
      await sleep(2000);
      await triggerGraphqlTraffic('after transactions navigation');
    }
    if (!lastAppCheck && Date.now() - start > 20000 && Date.now() - start < 20500) {
      await triggerGraphqlTraffic('second explicit GraphQL trigger');
    }
  }
  if (!lastAppCheck) {
    const current = await currentHref();
    throw new Error(`timed out waiting for x-firebase-appcheck at ${current || 'unknown-url'} via ${versionUrl}`);
  }
  if (lastGraphqlHeaders) {
    writeSessionCache(lastGraphqlHeaders);
    await fetchGraphqlWithCapturedHeaders(lastGraphqlHeaders, 'browser target');
    try { ws.close(); } catch (_err) {}
    return;
  }

  const expression = `
    (async () => {
      const payloads = ${JSON.stringify(graphqlInputs)};
      const results = [];
      for (const payload of payloads) {
        const resp = await fetch('https://orchestration.baselane.com/graphql', {
          method: 'POST',
          credentials: 'include',
          headers: {
            'accept': '*/*',
            'content-type': 'application/json',
            'x-firebase-appcheck': ${JSON.stringify(lastAppCheck)},
            ...${JSON.stringify(requestHeaders)}
          },
          body: JSON.stringify(payload)
        });
        const text = await resp.text();
        if (resp.status !== 200) return JSON.stringify({ status: resp.status, text });
        const parsed = JSON.parse(text);
        results.push(parsed);
        if (parsed && parsed.errors) return JSON.stringify({ status: 200, text: JSON.stringify(parsed) });
      }
      return JSON.stringify({
        status: 200,
        text: JSON.stringify(results.length === 1 ? results[0] : {batchResults: results})
      });
    })()
  `;
  const result = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true }, sessionId, commandTimeoutMs);
  const outer = parseEvaluateJsonResult(result, 'GRAPHQL_EVALUATE');
  if (outer.status !== 200) throw new Error(`GRAPHQL_STATUS_${outer.status}: ${String(outer.text || '').slice(0, 500)}`);
  await writeStdoutLine(typeof outer.text === 'string' ? outer.text : JSON.stringify(outer));
  return;
}

main().catch(err => {
  console.error(errorMessage(err));
  process.exit(1);
});
