#!/usr/bin/env node
const fs = require('fs');
const path = require('path');
const {execFileSync} = require('child_process');
// Use ws package for custom header support (needed for Docker→Windows CDP)
let WebSocketImpl;
try { WebSocketImpl = require('ws'); }
catch (_e) { WebSocketImpl = WebSocket; }

const OPENCLAW_ROOT = process.env.OPENCLAW_ROOT || path.join(process.env.HOME || '', '.openclaw');
const ROOT = process.env.WORKSPACE_ROOT || path.join(OPENCLAW_ROOT, 'workspace');
const REPORTS = path.join(ROOT, 'reports');
const versionUrl = process.env.BASELANE_CDP_VERSION_URL || 'http://127.0.0.1:9222/json/version';
const targetUrl = process.env.BASELANE_LOGIN_WAIT_URL || 'https://app.baselane.com/transactions';
const forceLogin = process.env.BASELANE_FORCE_LOGIN === '1';
const reuseOnly = process.env.BASELANE_LOGIN_REUSE_ONLY === '1';
const timeoutMs = Number(process.env.BASELANE_LOGIN_WAIT_TIMEOUT_MS || 180000);
const authenticatedContentTimeoutMs = Number(process.env.BASELANE_AUTHENTICATED_CONTENT_TIMEOUT_MS || 120000);
const waitMs = Number(process.env.BASELANE_LOGIN_WAIT_MS || 900000);
const cdpCommandTimeoutMs = Math.max(1000, Number(process.env.BASELANE_CDP_COMMAND_TIMEOUT_MS || 15000));
const reportPath = path.join(REPORTS, 'baselane_login_wait_report.json');
const loginDeadlineAt = Date.now() + Math.max(1000, timeoutMs);
let lastPageDiagnostic = {};
let lastFailureReason = '';
let lastHardRefresh = null;

function parseEnvFile(file) {
  const out = {};
  if (!fs.existsSync(file)) return out;
  for (const line of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    if (!line || line.trim().startsWith('#') || !line.includes('=')) continue;
    const idx = line.indexOf('=');
    out[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
  }
  return out;
}

function ensureCreds() {
  const envFile = path.join(ROOT, '.secrets', 'bw.env');
  // execFileSync(path.join(ROOT, 'scripts', 'bw_ensure_session.sh'), {env: {...process.env, BW_ENV: envFile}, stdio: ['ignore','pipe','pipe']});
  // BW_SESSION lives in /home/digit/.openclaw/.env (live, unlocks the active bw vault).
  // bw.env provides BASELANE_ITEM_ID + fallback values; .openclaw/.env must override stale fallback sessions.
  const openclawEnv = path.join(ROOT, '..', '.env');
  const openclawMerged = fs.existsSync(openclawEnv) ? parseEnvFile(openclawEnv) : {};
  const merged = {...process.env, ...parseEnvFile(envFile), ...openclawMerged};
  const session = String(merged.BW_SESSION || '').trim();
  const itemId = String(merged.BASELANE_ITEM_ID || '').trim();
  if (!itemId) throw new Error('Missing BASELANE_ITEM_ID');
  const bwEnv = {
    ...process.env,
    ...merged,
    BW_NOINTERACTION: 'true',
    PATH: `${process.env.HOME || ''}/.local/bin:/home/digit/.npm-global/bin:${process.env.HOME || ''}/.npm-global/bin:${process.env.PATH || ''}`,
  };
  if (bwEnv.BW_CLIENT_ID && !bwEnv.BW_CLIENTID) bwEnv.BW_CLIENTID = bwEnv.BW_CLIENT_ID;
  if (bwEnv.BW_CLIENT_SECRET && !bwEnv.BW_CLIENTSECRET) bwEnv.BW_CLIENTSECRET = bwEnv.BW_CLIENT_SECRET;
  function bwGetItem(bwSession) {
    const bwArgs = ['get', 'item', itemId];
    if (bwSession) bwArgs.push('--session', bwSession);
    return execFileSync('bw', bwArgs, {encoding: 'utf8', env: {...bwEnv, BW_SESSION: bwSession || bwEnv.BW_SESSION || ''}});
  }
  function bwUnlockSession() {
    if (!String(bwEnv.BW_MASTER_KEY || '').trim()) return '';
    if (bwEnv.BW_CLIENTID && bwEnv.BW_CLIENTSECRET) {
      try { execFileSync('bw', ['login', '--apikey'], {encoding: 'utf8', env: bwEnv}); } catch (e) {}
    }
    return String(execFileSync('bw', ['unlock', '--passwordenv', 'BW_MASTER_KEY', '--raw'], {encoding: 'utf8', env: bwEnv})).trim();
  }
  let itemJson = '';
  try {
    itemJson = bwGetItem(session);
  } catch (firstError) {
    const unlockedSession = bwUnlockSession();
    if (!unlockedSession) throw firstError;
    itemJson = bwGetItem(unlockedSession);
  }
  const item = JSON.parse(itemJson);
  const login = item.login || {};
  const email = String(login.username || '').trim();
  const password = String(login.password || '').trim();
  if (!email || !password) throw new Error('Bitwarden item missing username/password');
  return {email, password};
}

function redactSecrets(text) {
  return String(text || '')
    .replace(/--session\s+\S+/g, '--session <redacted>')
    .replace(/BW_SESSION=[^\s"'`]+/g, 'BW_SESSION=<redacted>');
}

function authNextAction(reason) {
  const monthlyTarget = /\/banking\/statements/i.test(String(targetUrl || ''));
  if (reason === 'baselane_login_recaptcha_required') {
    return monthlyTarget
      ? 'A CAPTCHA authentication state is open in the visible CDP tab. After it is satisfied, rerun `bash scripts/baselane_seed_session.sh`, then `bash scripts/baselane_financials_post_auth_resume.sh`.'
      : 'A CAPTCHA authentication state is open in the visible CDP tab. After it is satisfied, rerun `bash scripts/baselane_seed_session.sh`.';
  }
  if (reason === 'baselane_login_mfa_required') {
    return monthlyTarget
      ? 'An MFA authentication state is open in the visible CDP tab. After it is satisfied, rerun `bash scripts/baselane_seed_session.sh`, then `bash scripts/baselane_financials_post_auth_resume.sh`.'
      : 'An MFA authentication state is open in the visible CDP tab. After it is satisfied, rerun `bash scripts/baselane_seed_session.sh`.';
  }
  return monthlyTarget
    ? 'Hard refresh Baselane login, or close/open a fresh Baselane tab; finish login, then rerun `bash scripts/baselane_financials_post_auth_resume.sh`.'
    : 'Hard refresh Baselane login, or close/open a fresh Baselane tab; finish login, then rerun the Baselane gate refresh.';
}

console.log("[baselane-cdp] VERSION_URL=", versionUrl);

async function main() {
  fs.mkdirSync(REPORTS, {recursive: true});
  const {email, password} = ensureCreds();
  // Fetch CDP version - use http.get for reliable header handling (Host: localhost required for Docker→Windows CDP)
  const _cdpUrl = new URL(versionUrl);
  const _cdpIsRemote = _cdpUrl.hostname !== 'localhost' && _cdpUrl.hostname !== '127.0.0.1' && _cdpUrl.hostname !== '[::1]';
  // Node's fetch doesn't reliably send Host header overrides; use http.get for remote CDP
  let version;
  if (_cdpIsRemote) {
    const { get } = require('http');
    version = await new Promise((resolve, reject) => {
      get({ hostname: _cdpUrl.hostname, port: _cdpUrl.port || 80, path: _cdpUrl.pathname, headers: { Host: 'localhost' } }, r => {
        let d = '';
        r.on('data', c => d += c);
        r.on('end', () => { try { resolve(JSON.parse(d)); } catch (e) { reject(e); } });
      }).on('error', reject);
    });
  } else {
    version = await (await fetch(versionUrl)).json();
  }
  // Rewrite ws://localhost or ws://127.0.0.1 URLs to use the CDP host from BASELANE_CDP_VERSION_URL
  // so WebSocket connections work from inside Docker containers (host.docker.internal)
  let wsUrl = version.webSocketDebuggerUrl;
  if (process.env.BASELANE_CDP_VERSION_URL) {
    try {
      const cdpUrl = new URL(process.env.BASELANE_CDP_VERSION_URL);
      const cdpHost = cdpUrl.hostname;
      const cdpPort = cdpUrl.port || (cdpUrl.protocol === 'https:' ? '443' : '80');
      wsUrl = wsUrl.replace(/ws:\/\/(localhost|127\.0\.0\.1|\[::1\])(:\d+)?\//, `ws://${cdpHost}:${cdpPort}/`);
    } catch (_e) { /* leave wsUrl unchanged if URL parsing fails */ }
  }
  const wsOpts = {};
  try {
    const _wu = new URL(wsUrl);
    if (_wu.hostname !== 'localhost' && _wu.hostname !== '127.0.0.1' && _wu.hostname !== '[::1]') {
      wsOpts.headers = { Host: 'localhost' };
    }
  } catch (_e) { /* ignore */ }
  const ws = new WebSocketImpl(wsUrl, wsOpts);
  let id = 0;
  const pending = new Map();
  const sessions = new Map();

  function send(method, params = {}, sessionId) {
    const commandId = ++id;
    const msg = {id: commandId, method, params};
    if (sessionId) msg.sessionId = sessionId;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(commandId);
        reject(new Error(`timeout waiting for CDP ${method}`));
      }, cdpCommandTimeoutMs);
      pending.set(commandId, {resolve, reject, timer});
      try {
        ws.send(JSON.stringify(msg));
      } catch (err) {
        clearTimeout(timer);
        pending.delete(commandId);
        reject(err);
      }
    });
  }

  // Handle messages from both ws package (Buffer) and built-in WebSocket (ev.data)
  function handleWsMessage(rawData) {
    const text = typeof rawData === 'string' ? rawData : (rawData && rawData.toString ? rawData.toString() : String(rawData));
    const msg = JSON.parse(text);
    if (msg.id) {
      const p = pending.get(msg.id);
      if (p) {
        pending.delete(msg.id);
        clearTimeout(p.timer);
        if (msg.error) p.reject(new Error(JSON.stringify(msg.error))); else p.resolve(msg.result);
      }
      return;
    }
    if (msg.method === 'Target.attachedToTarget') {
      sessions.set(msg.params.targetInfo.targetId, msg.params.sessionId);
    }
  }

  // Wire message handler: ws package uses 'message' event with Buffer; built-in uses onmessage with ev.data
  if (typeof ws.on === 'function') {
    ws.on('message', handleWsMessage);
  } else {
    ws.onmessage = (ev) => handleWsMessage(ev.data);
  }

  // Wait for connection: ws package uses 'open' event; built-in uses onopen
  await new Promise((resolve, reject) => {
    if (typeof ws.on === 'function') {
      ws.on('open', () => { resolve(); });
      ws.on('error', (err) => { reject(err); });
    } else {
      ws.onopen = resolve;
      ws.onerror = reject;
    }
  });
  await send('Target.setAutoAttach', {autoAttach: true, waitForDebuggerOnStart: false, flatten: true});
  const targets = await send('Target.getTargets', {});
  // Close duplicate Baselane tabs to prevent tab buildup from cron retries
  const baselanePages = (targets.targetInfos || []).filter(t => t.type === 'page' && t.url && t.url.includes('app.baselane.com'));
  if (baselanePages.length > 1) {
    console.log(`[CDP] Found ${baselanePages.length} Baselane tabs — closing ${baselanePages.length - 1} duplicates`);
    for (let i = 1; i < baselanePages.length; i++) {
      try { await send('Target.closeTarget', {targetId: baselanePages[i].targetId}); } catch(e) {}
    }
  }
  // Reuse ANY existing Baselane tab (including login pages) — never create duplicates
  const candidate = (targets.targetInfos || []).find(t => t.type === 'page' && t.url && t.url.includes('app.baselane.com'));

  let sessionId = null;
  let activeTargetId = null;
  if (candidate) {
    console.log('[CDP] Reusing existing Baselane tab:', candidate.url.substring(0, 60));
    const attached = await send('Target.attachToTarget', {targetId: candidate.targetId, flatten: true});
    sessionId = attached.sessionId;
    activeTargetId = candidate.targetId;
    // If it's a login page, navigate to the target URL
    if (candidate.url.includes('/login')) {
      console.log('[CDP] Tab is on login page — navigating to', targetUrl);
      await send('Page.navigate', {url: targetUrl}, sessionId);
      await sleep(3000);
    }
  } else {
    if (reuseOnly) {
      throw new Error('BASELANE_LOGIN_REUSE_ONLY: no existing Baselane tab to reuse');
    }
    console.log('[CDP] No existing Baselane tab — creating one');
    const {targetId} = await send('Target.createTarget', {url: targetUrl});
    activeTargetId = targetId;
    for (let i = 0; i < 300; i++) {
      sessionId = sessions.get(targetId);
      if (sessionId) break;
      await new Promise(r => setTimeout(r, 100));
    }
    if (!sessionId) throw new Error('no session attached');
  }

  async function enableCurrentSession() {
    await send('Runtime.enable', {}, sessionId);
    try {
      await send('Page.enable', {}, sessionId);
    } catch (err) {
      console.log(`[CDP] Page domain unavailable; continuing with Runtime (${String(err && err.message || err).slice(0, 160)})`);
    }
    try {
      await send('Network.enable', {}, sessionId);
    } catch (err) {
      console.log(`[CDP] Network domain unavailable; continuing with DOM probes (${String(err && err.message || err).slice(0, 160)})`);
    }
  }

  async function replaceActiveTarget(reason) {
    if (reuseOnly) {
      throw new Error(`BASELANE_LOGIN_REUSE_ONLY: ${reason}; not opening a fresh Baselane tab`);
    }
    console.log(`[CDP] ${reason}; opening fresh Baselane tab`);
    const staleTargetId = activeTargetId;
    const {targetId} = await send('Target.createTarget', {url: targetUrl});
    const attached = await send('Target.attachToTarget', {targetId, flatten: true});
    sessionId = attached.sessionId;
    activeTargetId = targetId;
    await enableCurrentSession();
    if (staleTargetId && staleTargetId !== targetId) {
      try { await send('Target.closeTarget', {targetId: staleTargetId}); } catch (_e) {}
    }
  }

  try {
    await enableCurrentSession();
  } catch (err) {
    await replaceActiveTarget(`CDP session did not enable cleanly (${String(err && err.message || err).slice(0, 160)})`);
  }

  async function evalExpr(expression, awaitPromise = true) {
    const res = await send('Runtime.evaluate', {expression, awaitPromise, returnByValue: true}, sessionId);
    return res.result ? res.result.value : undefined;
  }

  function remainingLoginTimeoutMs(requestedMs = timeoutMs) {
    const remainingMs = loginDeadlineAt - Date.now();
    if (remainingMs <= 0) return 0;
    return Math.min(requestedMs, remainingMs);
  }

  async function sleep(requestedMs) {
    const boundedMs = remainingLoginTimeoutMs(requestedMs);
    if (boundedMs <= 0) throw new Error('timeout waiting for Baselane login deadline');
    await new Promise(r => setTimeout(r, boundedMs));
  }

  async function capturePageDiagnostic(label) {
    lastPageDiagnostic = await evalExpr(`(() => {
      const href = String(location.href || '');
      const title = String(document.title || '');
      const text = String(document.body && document.body.innerText || '').trim();
      const visible = s => Array.from(document.querySelectorAll(s)).some(el => {
        const style = window.getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return style.visibility !== 'hidden' && style.display !== 'none' && !el.disabled && r.width > 0 && r.height > 0;
      });
      const loginInputsPresent = visible('input[name="email"], input#email, input[type="email"], input[autocomplete="email"]')
        || visible('input[name="password"], input#password, input[type="password"], input[autocomplete="current-password"]');
      const recaptchaPresent = Array.from(document.querySelectorAll(
        'iframe[src*="recaptcha"], iframe[title*="recaptcha" i]'
      )).some(el => {
        const style = window.getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return style.visibility !== 'hidden'
          && style.display !== 'none'
          && r.width > 20
          && r.height > 20;
      }) || /verify you are human|complete (?:the )?captcha|captcha challenge/i.test(text);
      const mfaPresent = visible('input[autocomplete="one-time-code"], input[name*="otp" i], input[id*="otp" i], input[inputmode="numeric"][maxlength="6"]')
        || /enter (the )?(verification|security) code|two[- ]factor|multi[- ]factor|2-step verification/i.test(text);
      const challengeType = recaptchaPresent ? 'captcha' : mfaPresent ? 'mfa' : null;
      return {
        label: ${JSON.stringify(label)},
        href,
        title,
        onLoginUrl: href.includes('/login') || href.includes('session-expired') || href.includes('/access-denied') || href.includes('/error'),
        loginInputsPresent,
        recaptchaPresent,
        mfaPresent,
        challengeType,
        bodyExcerpt: text.slice(0, 500),
      };
    })()`);
    return lastPageDiagnostic;
  }

  async function waitFor(pred, label, ms = timeoutMs) {
    const start = Date.now();
    const boundedMs = remainingLoginTimeoutMs(ms);
    if (boundedMs <= 0) throw new Error(`timeout waiting for ${label}`);
    while (Date.now() - start < boundedMs) {
      let v = null;
      try { v = await Promise.resolve(pred()); } catch (_e) { v = null; }
      if (v) return v;
      await sleep(500);
    }
    throw new Error(`timeout waiting for ${label}`);
  }

  async function loginScreenState() {
    return await evalExpr(`(() => {
      const href = String(location.href || '');
      const text = String(document.body && document.body.innerText || '');
      const visible = s => Array.from(document.querySelectorAll(s)).some(el => {
        const style = window.getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return style.visibility !== 'hidden' && style.display !== 'none' && !el.disabled && r.width > 0 && r.height > 0;
      });
      const emailVisible = visible('input[name="email"], input#email, input[type="email"], input[autocomplete="email"]');
      const passwordVisible = visible('input[name="password"], input#password, input[type="password"], input[autocomplete="current-password"]');
      return {
        href,
        onLoginUrl: href.includes('/login') || href.includes('session-expired') || href.includes('/access-denied') || href.includes('/error'),
        loginInputsPresent: emailVisible && passwordVisible,
        sessionExpiredText: /session expired/i.test(text),
      };
    })()`);
  }

  async function openFreshBaselaneTab(reason) {
    if (reuseOnly) {
      throw new Error(`BASELANE_LOGIN_REUSE_ONLY: ${reason}; not opening a fresh Baselane tab`);
    }
    console.log(`[CDP] ${reason}; closing stale tab and opening fresh Baselane tab`);
    await replaceActiveTarget(reason);
    await waitFor(() => evalExpr(`document.readyState === 'complete' || document.readyState === 'interactive'`), 'fresh tab dom ready', 30000);
  }

  async function recoverLoginScreenIfNeeded() {
    let state = await loginScreenState();
    if (!state || !(state.onLoginUrl || state.loginInputsPresent || state.sessionExpiredText)) return state;

    console.log('[CDP] Baselane login/session screen detected — hard refresh first');
    const hardRefresh = {
      attempted: true,
      cache_clear_dispatched: false,
      page_reload_dispatched: false,
      cache_clear_error: null,
      page_reload_error: null,
    };
    try {
      await send('Network.clearBrowserCache', {}, sessionId);
      hardRefresh.cache_clear_dispatched = true;
    } catch (err) {
      hardRefresh.cache_clear_error = String(err && err.message || err).slice(0, 240);
    }
    try {
      await send('Page.reload', {ignoreCache: true}, sessionId);
      hardRefresh.page_reload_dispatched = true;
    } catch (err) {
      hardRefresh.page_reload_error = String(err && err.message || err).slice(0, 240);
    }
    lastHardRefresh = hardRefresh;
    await sleep(3000);
    await waitFor(() => evalExpr(`document.readyState === 'complete' || document.readyState === 'interactive'`), 'post-refresh dom ready', 30000).catch(() => {});
    state = await loginScreenState();
    if (state && (state.onLoginUrl || state.loginInputsPresent || state.sessionExpiredText)) {
      await openFreshBaselaneTab('Baselane remained on login/session screen after hard refresh');
      state = await loginScreenState();
    }
    return state;
  }

  async function getRect(selector) {
    return await evalExpr(`(() => {
      const els = Array.from(document.querySelectorAll(${JSON.stringify(selector)}));
      for (const el of els) {
        const style = window.getComputedStyle(el);
        const r = el.getBoundingClientRect();
        if (style.visibility !== 'hidden' && style.display !== 'none' && !el.disabled && r.width > 0 && r.height > 0) {
          return {x:r.left + r.width/2, y:r.top + r.height/2, width:r.width, height:r.height};
        }
      }
      return null;
    })()`);
  }

  async function clickAt(x, y) {
    await send('Input.dispatchMouseEvent', {type:'mouseMoved', x, y, button:'none', buttons:1}, sessionId);
    await send('Input.dispatchMouseEvent', {type:'mousePressed', x, y, button:'left', buttons:1, clickCount:1}, sessionId);
    await send('Input.dispatchMouseEvent', {type:'mouseReleased', x, y, button:'left', buttons:0, clickCount:1}, sessionId);
  }

  async function clearAndType(selector, text) {
    const rect = await getRect(selector);
    if (rect) {
      await clickAt(rect.x, rect.y);
    }
    const filled = await evalExpr(`(() => {
      const els = Array.from(document.querySelectorAll(${JSON.stringify(selector)}));
      const el = els.find(candidate => {
        const style = window.getComputedStyle(candidate);
        const r = candidate.getBoundingClientRect();
        return style.visibility !== 'hidden' && style.display !== 'none' && !candidate.disabled && r.width > 0 && r.height > 0;
      }) || els[0];
      if (!el) return false;
      el.scrollIntoView({block:'center', inline:'center'});
      el.focus();
      const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
      if (setter) setter.call(el, ${JSON.stringify(text)});
      else el.value = ${JSON.stringify(text)};
      el.dispatchEvent(new Event('input', {bubbles:true}));
      el.dispatchEvent(new Event('change', {bubbles:true}));
      return true;
    })()`);
    if (!filled) throw new Error(`missing input for ${selector}`);
  }

  await send('Page.navigate', {url: targetUrl}, sessionId).catch(() => {});
  try {
    await waitFor(() => evalExpr(`document.readyState === 'complete' || document.readyState === 'interactive'`), 'initial dom ready', 30000);
  } catch (firstReadyError) {
    lastFailureReason = 'baselane_initial_dom_ready_timeout';
    await replaceActiveTarget(`Baselane initial DOM probe timed out (${String(firstReadyError && firstReadyError.message || firstReadyError).slice(0, 160)})`);
    await send('Page.navigate', {url: targetUrl}, sessionId).catch(() => {});
    try {
      await waitFor(() => evalExpr(`document.readyState === 'complete' || document.readyState === 'interactive'`), 'initial dom ready after fresh target', 30000);
    } catch (secondReadyError) {
      throw new Error(`${firstReadyError.message}; fresh target retry failed: ${secondReadyError.message}`);
    }
  }
  await recoverLoginScreenIfNeeded();
  await capturePageDiagnostic('after_initial_recovery').catch(() => {});

  let currentHref = await evalExpr('location.href');
  let loginInputsPresent = await evalExpr(`(() => {
    const visible = s => Array.from(document.querySelectorAll(s)).some(el => {
      const style = window.getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return style.visibility !== 'hidden' && style.display !== 'none' && !el.disabled && r.width > 0 && r.height > 0;
    });
    return visible('input[name="email"], input#email, input[type="email"], input[autocomplete="email"]') && visible('input[name="password"], input#password, input[type="password"], input[autocomplete="current-password"]');
  })()`);
  let loginResult = 'existing_authenticated_session';
  if (forceLogin || String(currentHref).includes('/login') || String(currentHref).includes('session-expired') || String(currentHref).includes('/access-denied') || String(currentHref).includes('/error') || loginInputsPresent) {
    // A transactions navigation can briefly render a complete/loading shell
    // before redirecting to /login.  With forceLogin enabled, that race used
    // to let the input preflight observe the eventual login form and then let
    // clearAndType run against the replacing document.  Navigate explicitly
    // to the stable login route before touching credentials.
    if (forceLogin && (!String(currentHref).includes('/login') || !loginInputsPresent)) {
      await send('Page.navigate', {url: 'https://app.baselane.com/login'}, sessionId).catch(() => {});
      const forcedRouteState = await waitFor(() => evalExpr(`(() => {
        const href = String(location.href || '');
        const text = String(document.body && document.body.innerText || '').trim();
        const visible = s => Array.from(document.querySelectorAll(s)).some(el => {
          const style = window.getComputedStyle(el);
          const r = el.getBoundingClientRect();
          return style.visibility !== 'hidden' && style.display !== 'none' && !el.disabled && r.width > 0 && r.height > 0;
        });
        const hasInputs = href.includes('/login')
          && visible('input[name="email"], input#email, input[type="email"], input[autocomplete="email"]')
          && visible('input[name="password"], input#password, input[type="password"], input[autocomplete="current-password"]');
        if (hasInputs) return 'login_inputs';
        const authenticated = href.startsWith('https://app.baselane.com/')
          && !href.includes('/login')
          && !href.includes('/access-denied')
          && !href.includes('/error')
          && text
          && !/^Loading\\.*$/i.test(text)
          && !/session expired/i.test(text);
        return authenticated ? 'authenticated_redirect' : false;
      })()`), 'stable forced login route', 30000);
      // Require the same document to remain ready across a short settling
      // interval so a stale SPA shell cannot satisfy the probe transiently.
      await sleep(500);
      currentHref = await evalExpr('location.href');
      loginInputsPresent = forcedRouteState === 'login_inputs' && String(currentHref).includes('/login');
    }
    await evalExpr(`(() => {
      const hide = s => document.querySelectorAll(s).forEach(el => { el.disabled = true; el.style.pointerEvents='none'; el.style.display='none'; });
      hide('#signInButtonAppleSSO, #signInButtonSSO, button[id*="Apple"], button[id*="SSO"], button[id*="Google"]');
      return true;
    })()`);

    const inputsOk = loginInputsPresent && await waitFor(() => evalExpr(`(() => {
      const visible = s => Array.from(document.querySelectorAll(s)).some(el => {
        const style = window.getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return style.visibility !== 'hidden' && style.display !== 'none' && !el.disabled && r.width > 0 && r.height > 0;
      });
      return visible('input[name="email"], input#email, input[type="email"], input[autocomplete="email"]') && visible('input[name="password"], input#password, input[type="password"], input[autocomplete="current-password"]');
    })()`), 'login inputs', 30000).catch(()=>false);
    if (!inputsOk) {
      // assume already authenticated or redirected; skip login
    } else {
    await clearAndType('input[name="email"], input#email, input[type="email"], input[autocomplete="email"]', email);
    await clearAndType('input[name="password"], input#password, input[type="password"], input[autocomplete="current-password"]', password);
    const signInRect = await getRect('#signInButton, button[type="submit"]');
    if (!signInRect) throw new Error('missing sign in button');
    await clickAt(signInRect.x, signInRect.y);
    loginResult = 'real_click_and_insertText';

    await waitFor(async () => {
      const href = await evalExpr('location.href');
      if (!href) return false;
      return !String(href).includes('/login') && !String(href).includes('/access-denied') && !String(href).includes('/error') && String(href).startsWith('https://app.baselane.com/') ? href : false;
    }, 'post-login navigation', 90000).catch(async err => {
      const state = await recoverLoginScreenIfNeeded();
      if (state && !(state.onLoginUrl || state.loginInputsPresent || state.sessionExpiredText)) {
        return state.href;
      }
      const diagnostic = await capturePageDiagnostic('post_login_navigation_timeout').catch(() => lastPageDiagnostic || {});
      if (diagnostic && diagnostic.recaptchaPresent) {
        lastFailureReason = 'baselane_login_recaptcha_required';
        throw new Error('baselane_login_recaptcha_required: post-login navigation blocked');
      }
      if (diagnostic && diagnostic.mfaPresent) {
        lastFailureReason = 'baselane_login_mfa_required';
        throw new Error('baselane_login_mfa_required: post-login navigation blocked');
      }
      if (diagnostic && (diagnostic.onLoginUrl || diagnostic.loginInputsPresent)) {
        lastFailureReason = 'baselane_login_required';
        throw new Error('baselane_login_required: post-login navigation blocked');
      }
      throw err;
    });
    }
  }

  await send('Page.navigate', {url: targetUrl}, sessionId).catch(() => {});
  await waitFor(async () => {
    const href = await evalExpr('location.href');
    return String(href).startsWith('https://app.baselane.com/') && !String(href).includes('/login') && !String(href).includes('/access-denied') && !String(href).includes('/error') ? href : false;
  }, 'target page', 60000).catch(async err => {
    const state = await recoverLoginScreenIfNeeded();
    if (state && !(state.onLoginUrl || state.loginInputsPresent || state.sessionExpiredText)) {
      return state.href;
    }
    const diagnostic = await capturePageDiagnostic('target_page_timeout').catch(() => lastPageDiagnostic || {});
    if (diagnostic && diagnostic.recaptchaPresent) {
      lastFailureReason = 'baselane_login_recaptcha_required';
      throw new Error('baselane_login_recaptcha_required: target page blocked');
    }
    if (diagnostic && diagnostic.mfaPresent) {
      lastFailureReason = 'baselane_login_mfa_required';
      throw new Error('baselane_login_mfa_required: target page blocked');
    }
    if (diagnostic && (diagnostic.onLoginUrl || diagnostic.loginInputsPresent)) {
      lastFailureReason = 'baselane_login_required';
      throw new Error('baselane_login_required: target page blocked');
    }
    throw err;
  });

  await waitFor(async () => {
    return await evalExpr(`(() => {
      const href = String(location.href || '');
      const text = String(document.body && document.body.innerText || '').trim();
      const visible = s => Array.from(document.querySelectorAll(s)).some(el => {
        const style = window.getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return style.visibility !== 'hidden' && style.display !== 'none' && !el.disabled && r.width > 0 && r.height > 0;
      });
      const loginVisible = visible('input[name="email"], input#email, input[type="email"], input[autocomplete="email"]')
        || visible('input[name="password"], input#password, input[type="password"], input[autocomplete="current-password"]');
      if (!href.startsWith('https://app.baselane.com/') || href.includes('/login') || href.includes('/access-denied') || href.includes('/error') || loginVisible) return false;
      if (!text || /^Loading\\.*$/i.test(text) || /session expired|sign in/i.test(text)) return false;
      return {href, title: document.title, text: text.slice(0, 120)};
    })()`);
  }, 'authenticated app content', authenticatedContentTimeoutMs).catch(async err => {
    const diagnostic = await capturePageDiagnostic('authenticated_content_timeout').catch(() => lastPageDiagnostic || {});
    if (diagnostic && diagnostic.recaptchaPresent) {
      lastFailureReason = 'baselane_login_recaptcha_required';
      throw new Error('baselane_login_recaptcha_required: authenticated app content not confirmed');
    }
    if (diagnostic && diagnostic.mfaPresent) {
      lastFailureReason = 'baselane_login_mfa_required';
      throw new Error('baselane_login_mfa_required: authenticated app content not confirmed');
    }
    if (diagnostic && (diagnostic.onLoginUrl || diagnostic.loginInputsPresent)) {
      lastFailureReason = 'baselane_login_required';
      throw new Error('baselane_login_required: authenticated app content not confirmed');
    }
    throw err;
  });

  const finalUrl = await evalExpr('location.href');
  const title = await evalExpr('document.title');
  const bodyText = await evalExpr(`(document.body && document.body.innerText ? document.body.innerText.slice(0, 1000) : '')`);

  const report = {
    ok: true,
    status: 'ok',
    challenge_type: null,
    login_result: loginResult,
    target_id: activeTargetId,
    targetUrl: targetUrl,
    final_url: finalUrl,
    title,
    waited_ms: waitMs,
    checked_at: new Date().toISOString(),
    note: 'Logged in (if needed) and held authenticated Baselane UI target open for model-execution tasks.',
    body_excerpt: bodyText,
    page_diagnostic: lastPageDiagnostic,
    hard_refresh: lastHardRefresh,
  };
  fs.writeFileSync(reportPath, JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));

  await new Promise(r => setTimeout(r, waitMs));
  ws.close();
}

main().catch(err => {
  fs.mkdirSync(REPORTS, {recursive: true});
  const error = redactSecrets(String(err && err.stack || err));
  const reason = lastFailureReason
    || (lastPageDiagnostic.recaptchaPresent === true ? 'baselane_login_recaptcha_required'
      : lastPageDiagnostic.mfaPresent === true ? 'baselane_login_mfa_required'
      : error.includes('baselane_login_recaptcha_required') ? 'baselane_login_recaptcha_required'
      : error.includes('baselane_login_mfa_required') ? 'baselane_login_mfa_required'
      : error.includes('baselane_login_required') ? 'baselane_login_required'
        : error.includes('authenticated app content') ? 'baselane_authenticated_content_not_confirmed'
          : 'baselane_login_wait_failed');
  const challengeType = reason === 'baselane_login_recaptcha_required'
    ? 'captcha'
    : reason === 'baselane_login_mfa_required'
      ? 'mfa'
      : null;
  const report = {
    ok: false,
    status: challengeType ? 'authentication_challenge' : 'review',
    reason,
    challenge_type: challengeType,
    error,
    current_url: lastPageDiagnostic.href || '',
    title: lastPageDiagnostic.title || '',
    login_inputs_present: lastPageDiagnostic.loginInputsPresent === true,
    recaptcha_present: lastPageDiagnostic.recaptchaPresent === true,
    mfa_present: lastPageDiagnostic.mfaPresent === true,
    page_diagnostic: lastPageDiagnostic,
    hard_refresh: lastHardRefresh,
    checked_at: new Date().toISOString(),
    targetUrl,
    next_action: authNextAction(reason),
    resume_command: 'bash scripts/baselane_seed_session.sh',
  };
  fs.writeFileSync(reportPath, JSON.stringify(report, null, 2));
  console.error(report.error || String(err));
  process.exit(1);
});
