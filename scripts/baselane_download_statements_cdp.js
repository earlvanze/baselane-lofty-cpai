#!/usr/bin/env node
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

const ROOT = process.env.WORKSPACE_ROOT || path.join(process.env.HOME || '', '.openclaw', 'workspace');
const REPORTS = path.join(ROOT, 'reports');
const DOWNLOAD_DIR = process.env.DOWNLOAD_DIR || path.join(ROOT, 'baselane-statements');
const VERSION_URL = process.env.BASELANE_CDP_VERSION_URL || 'http://127.0.0.1:9222/json/version';
const TARGET_YEAR = String(process.env.BASELANE_STATEMENT_YEAR || '').trim();
const TARGET_MONTH = String(process.env.BASELANE_STATEMENT_MONTH || '').trim();
const MAX_LOAD_MORE = Number(process.env.BASELANE_MAX_LOAD_MORE || 10);
const MAX_DOWNLOADS = Number(process.env.BASELANE_MAX_DOWNLOADS || 300);
const CLICK_DELAY_MS = Number(process.env.BASELANE_CLICK_DELAY_MS || 1500);
const FINAL_WAIT_MS = Number(process.env.BASELANE_FINAL_WAIT_MS || 15000);
const CDP_COMMAND_TIMEOUT_MS = Number(process.env.BASELANE_CDP_COMMAND_TIMEOUT_MS || 30000);
const reportPath = path.join(REPORTS, 'baselane_statements_download_report.json');
const htmlDumpPath = path.join(REPORTS, 'baselane_statements_failure.html');
const screenshotPath = path.join(REPORTS, 'baselane_statements_failure.png');

function getCred(field) {
  return execFileSync('bash', [path.join(ROOT, 'scripts', 'bw_get_baselane.sh'), field], { encoding: 'utf8' }).trim();
}

function browserDownloadPath(localPath, browserName) {
  if (!/Windows/i.test(browserName || '')) return localPath;
  try {
    return execFileSync('wslpath', ['-w', localPath], { encoding: 'utf8' }).trim() || localPath;
  } catch {
    return localPath;
  }
}

let _captureFailureArtifacts = async () => {};

async function main() {
  fs.mkdirSync(REPORTS, { recursive: true });
  fs.mkdirSync(DOWNLOAD_DIR, { recursive: true });
  const before = new Set(fs.readdirSync(DOWNLOAD_DIR).filter(f => f.endsWith('.pdf')));

  const version = await (await fetch(VERSION_URL)).json();
  const browserDownloadDir = browserDownloadPath(DOWNLOAD_DIR, `${version.Browser || ''} ${version['User-Agent'] || ''}`);
  const ws = new WebSocket(version.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  const sessions = new Map();
  const networkEvents = [];
  const downloadEvents = [];
  const graphqlClickRequests = new Map();
  const graphqlClickResponses = [];
  let currentStage = 'init';

  function compactUrl(raw) {
    try {
      const url = new URL(String(raw || ''));
      url.search = url.search ? '?<redacted>' : '';
      return url.toString();
    } catch {
      return String(raw || '').slice(0, 300);
    }
  }

  function recordNetworkEvent(event) {
    if (!event || !event.url) return;
    if (!/statement|document|download|pdf|bank|orchestration/i.test(event.url)) return;
    networkEvents.push({
      ...event,
      url: compactUrl(event.url),
      stage: currentStage,
    });
    if (networkEvents.length > 100) networkEvents.shift();
  }

  function send(method, params = {}, sessionId) {
    const msg = { id: ++id, method, params };
    if (sessionId) msg.sessionId = sessionId;
    ws.send(JSON.stringify(msg));
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(msg.id);
        reject(new Error(`CDP command timed out: ${method} stage=${currentStage}`));
      }, CDP_COMMAND_TIMEOUT_MS);
      pending.set(msg.id, {
        resolve: value => {
          clearTimeout(timer);
          resolve(value);
        },
        reject: error => {
          clearTimeout(timer);
          reject(error);
        }
      });
    });
  }
  _captureFailureArtifacts = captureFailureArtifacts;

  ws.onmessage = ev => {
    const msg = JSON.parse(ev.data);
    if (msg.id) {
      const p = pending.get(msg.id);
      if (p) {
        pending.delete(msg.id);
        if (msg.error) p.reject(new Error(JSON.stringify(msg.error))); else p.resolve(msg.result);
      }
      return;
    }
    if (msg.method === 'Target.attachedToTarget') sessions.set(msg.params.targetInfo.targetId, msg.params.sessionId);
    if (msg.method === 'Network.requestWillBeSent') {
      if (
        currentStage === 'click-downloads' &&
        String(msg.params.request?.url || '').includes('/graphql')
      ) {
        let parsed = {};
        try { parsed = JSON.parse(msg.params.request?.postData || '{}'); } catch {}
        graphqlClickRequests.set(msg.params.requestId, {
          request_id: msg.params.requestId,
          operation_name: parsed.operationName || null,
          variable_keys: Object.keys(parsed.variables || {}),
          variables: Object.fromEntries(
            Object.entries(parsed.variables || {}).filter(([_key, value]) => (
              value === null || ['string', 'number', 'boolean'].includes(typeof value)
            ))
          ),
        });
      }
      recordNetworkEvent({
        type: 'request',
        method: msg.params.request?.method,
        url: msg.params.request?.url,
        resource_type: msg.params.type,
      });
    }
    if (msg.method === 'Network.responseReceived') {
      if (
        currentStage === 'click-downloads' &&
        graphqlClickRequests.has(msg.params.requestId)
      ) {
        graphqlClickResponses.push({
          request_id: msg.params.requestId,
          status: msg.params.response?.status,
          mime_type: msg.params.response?.mimeType,
        });
      }
      recordNetworkEvent({
        type: 'response',
        status: msg.params.response?.status,
        url: msg.params.response?.url,
        mime_type: msg.params.response?.mimeType,
        resource_type: msg.params.type,
      });
    }
    if (msg.method === 'Network.loadingFailed') {
      recordNetworkEvent({
        type: 'loading_failed',
        url: msg.params.requestId,
        error_text: msg.params.errorText,
        blocked_reason: msg.params.blockedReason,
      });
    }
    if (msg.method === 'Page.downloadWillBegin') {
      downloadEvents.push({
        type: 'download_will_begin',
        url: compactUrl(msg.params.url),
        suggested_filename: msg.params.suggestedFilename,
      });
    }
    if (msg.method === 'Page.downloadProgress') {
      downloadEvents.push({
        type: 'download_progress',
        state: msg.params.state,
        total_bytes: msg.params.totalBytes,
        received_bytes: msg.params.receivedBytes,
      });
    }
  };

  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
  await send('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: false, flatten: true });

  const targets = await send('Target.getTargets', {});
  for (const t of (targets.targetInfos || [])) {
    if (t.type === 'page' && t.url && t.url.includes('app.baselane.com/login/something-went-wrong')) {
      try { await send('Target.closeTarget', { targetId: t.targetId }); } catch {}
    }
  }

  const existingAuthed = (targets.targetInfos || []).find(t =>
    t.type === 'page' &&
    t.url &&
    t.url.includes('app.baselane.com') &&
    !t.url.includes('/login') &&
    !t.url.includes('/session-expired')
  );
  let targetId = null;
  let sessionId = null;
  const usingExistingAuthedTab = Boolean(existingAuthed);
  const created = await send('Target.createTarget', { url: 'about:blank' });
  targetId = created.targetId;
  for (let i = 0; i < 120; i++) {
    sessionId = sessions.get(targetId);
    if (sessionId) break;
    await new Promise(r => setTimeout(r, 100));
  }
  if (!sessionId) {
    const attached = await send('Target.attachToTarget', { targetId, flatten: true });
    sessionId = attached.sessionId;
  }
  if (!sessionId) throw new Error('no session attached');

  await send('Page.enable', {}, sessionId);
  await send('Runtime.enable', {}, sessionId);
  await send('Network.enable', {}, sessionId);
  await send('Browser.setDownloadBehavior', { behavior: 'allow', downloadPath: browserDownloadDir, eventsEnabled: true });

  async function evalExpr(expression, awaitPromise = true) {
    const res = await send('Runtime.evaluate', { expression, awaitPromise, returnByValue: true }, sessionId);
    return res.result ? res.result.value : undefined;
  }

  async function captureFailureArtifacts() {
    try {
      const html = await evalExpr('document.documentElement ? document.documentElement.outerHTML : ""');
      if (html) fs.writeFileSync(htmlDumpPath, html, 'utf8');
    } catch {}
    try {
      const shot = await send('Page.captureScreenshot', { format: 'png' }, sessionId);
      if (shot && shot.data) fs.writeFileSync(screenshotPath, Buffer.from(shot.data, 'base64'));
    } catch {}
  }

  if (!usingExistingAuthedTab) {
    const email = getCred('username');
    const password = getCred('password');
    currentStage = 'navigate-login';
    await send('Page.navigate', { url: 'https://app.baselane.com/login' }, sessionId);
    await new Promise(r => setTimeout(r, 3000));
    currentStage = 'wait-login-form';
    for (let i = 0; i < 120; i++) {
      const ok = await evalExpr(`Boolean(document.querySelector('input[name="email"],input#email,input[type="email"],input[autocomplete="email"]')) && Boolean(document.querySelector('input[name="password"],input#password,input[type="password"],input[autocomplete="current-password"]'))`);
      if (ok) break;
      await new Promise(r => setTimeout(r, 500));
    }

    const submitted = await evalExpr(`(() => {
      const e = document.querySelector('input[name="email"],input#email,input[type="email"],input[autocomplete="email"]');
      const p = document.querySelector('input[name="password"],input#password,input[type="password"],input[autocomplete="current-password"]');
      const btn = document.querySelector('#signInButton, button[type="submit"]');
      if (!e || !p || !btn) return { ok: false };
      const desc = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
      const setNative = (el, value) => { desc.set.call(el, value); el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true})); };
      setNative(e, ${JSON.stringify(email)});
      setNative(p, ${JSON.stringify(password)});
      btn.click();
      return { ok: true };
    })()`);
    if (!submitted || !submitted.ok) throw new Error('login form submission failed');

    for (let i = 0; i < 120; i++) {
      const href = await evalExpr('location.href');
      if (href && !String(href).includes('/login')) break;
      await new Promise(r => setTimeout(r, 1000));
    }
  }

  currentStage = 'navigate-statements';
  await send('Page.navigate', { url: 'https://app.baselane.com/banking/statements' }, sessionId);
  await new Promise(r => setTimeout(r, 5000));
  currentStage = 'wait-statements';
  for (let i = 0; i < 120; i++) {
    const s = await evalExpr(`({href:location.href,body:(document.body?.innerText||'').slice(0,1000)})`);
    if (String(s.href).includes('/login')) throw new Error('fell back to login on statements');
    if (String(s.href).includes('/banking/statements') && /Account Name[\s\S]*Statement Period[\s\S]*Download/.test(s.body || '')) break;
    await new Promise(r => setTimeout(r, 1000));
  }

  if (TARGET_YEAR) {
    currentStage = 'filter-year';
    await evalExpr(`(async () => {
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      const open = Array.from(document.querySelectorAll('button')).find(b => (b.innerText||b.textContent||'').trim() === 'All' && b.className.includes('chakra-menu__menu-button'));
      if (open) open.click();
      await sleep(400);
      const item = Array.from(document.querySelectorAll('button[value]')).find(b => b.value === ${JSON.stringify(TARGET_YEAR)});
      if (item) item.click();
      await sleep(800);
      return true;
    })()`);
  }

  if (TARGET_MONTH) {
    currentStage = 'filter-month';
    await evalExpr(`(async () => {
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      const dropdowns = Array.from(document.querySelectorAll('button')).filter(b => (b.innerText||b.textContent||'').trim() === 'All' && b.className.includes('chakra-menu__menu-button'));
      const open = dropdowns[1];
      if (open) open.click();
      await sleep(400);
      const item = Array.from(document.querySelectorAll('button[value]')).find(b => b.value === ${JSON.stringify(TARGET_MONTH)});
      if (item) item.click();
      await sleep(800);
      return true;
    })()`);
  }

  currentStage = 'click-downloads';
  let clickedLoadMore = 0;
  for (let i = 0; i < MAX_LOAD_MORE; i++) {
    const clicked = await evalExpr(`(() => {
      const loadMore = Array.from(document.querySelectorAll('button')).find(b => /load more/i.test((b.innerText||b.textContent||'').trim()));
      if (!loadMore) return false;
      loadMore.click();
      return true;
    })()`);
    if (!clicked) break;
    clickedLoadMore++;
    await new Promise(r => setTimeout(r, 1800));
  }
  const discovery = await evalExpr(`(() => {
    const values = Array.from(document.querySelectorAll('button[value]')).filter(b => (b.value||'').includes(',') && b.querySelector('svg')).map(b => b.value);
    const button_texts = Array.from(document.querySelectorAll('button')).slice(0, 80).map(b => (b.innerText||b.textContent||'').trim()).filter(Boolean);
    return {
      values,
      href: location.href,
      body_sample: (document.body?.innerText || '').slice(0, 4000),
      button_texts,
    };
  })()`);
  const discoveredValues = Array.isArray(discovery.values) ? discovery.values : [];
  const values = discoveredValues.slice(0, MAX_DOWNLOADS);
  let clickedButtons = 0;
  const clickResults = [];
  for (const value of values) {
    const target = await evalExpr(`(() => {
      const btn = Array.from(document.querySelectorAll('button[value]'))
        .filter(b => (b.value||'').includes(',') && b.querySelector('svg'))
        .find(b => b.value === ${JSON.stringify(value)});
      if (!btn) return { ok: false, reason: 'button-not-found' };
      btn.scrollIntoView({ block: 'center', inline: 'center' });
      const rect = btn.getBoundingClientRect();
      return {
        ok: rect.width > 0 && rect.height > 0 && !btn.disabled,
        reason: btn.disabled ? 'button-disabled' : null,
        x: rect.left + rect.width / 2,
        y: rect.top + rect.height / 2,
        width: rect.width,
        height: rect.height
      };
    })()`);
    let clicked = false;
    let mode = 'none';
    if (target && target.ok) {
      await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: target.x, y: target.y, button: 'none', buttons: 0 }, sessionId);
      await send('Input.dispatchMouseEvent', { type: 'mousePressed', x: target.x, y: target.y, button: 'left', buttons: 1, clickCount: 1 }, sessionId);
      await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: target.x, y: target.y, button: 'left', buttons: 0, clickCount: 1 }, sessionId);
      clicked = true;
      mode = 'trusted_mouse';
    } else {
      clicked = await evalExpr(`(() => {
        const btn = Array.from(document.querySelectorAll('button[value]'))
          .filter(b => (b.value||'').includes(',') && b.querySelector('svg'))
          .find(b => b.value === ${JSON.stringify(value)});
        if (!btn || btn.disabled) return false;
        btn.click();
        return true;
      })()`);
      mode = clicked ? 'dom_click_fallback' : String(target?.reason || 'click-failed');
    }
    clickResults.push({ value, clicked: Boolean(clicked), mode });
    if (clicked) clickedButtons++;
    await new Promise(r => setTimeout(r, CLICK_DELAY_MS));
  }

  currentStage = 'wait-downloads';
  await new Promise(r => setTimeout(r, FINAL_WAIT_MS));
  const graphqlClickResults = [];
  for (const response of graphqlClickResponses) {
    const request = graphqlClickRequests.get(response.request_id) || {};
    const result = { ...request, ...response };
    try {
      const body = await send('Network.getResponseBody', { requestId: response.request_id }, sessionId);
      const parsed = JSON.parse(body.body || '{}');
      if (Array.isArray(parsed.errors)) {
        result.errors = parsed.errors.slice(0, 5).map(error => ({
          message: String(error.message || '').slice(0, 200),
          path: Array.isArray(error.path) ? error.path.map(String) : [],
          code: error.extensions?.code || null,
          service_name: error.extensions?.serviceName || null,
        }));
      }
      if (parsed.data && typeof parsed.data === 'object') {
        result.data_keys = Object.keys(parsed.data);
      }
    } catch (error) {
      result.body_error = String(error && error.message || error).slice(0, 200);
    }
    graphqlClickResults.push(result);
  }
  const after = fs.readdirSync(DOWNLOAD_DIR).filter(f => f.endsWith('.pdf'));
  const newFiles = after.filter(f => !before.has(f));
  const ok = newFiles.length > 0;
  if (!ok) await captureFailureArtifacts();
  const graphqlError = graphqlClickResults.find(result => Array.isArray(result.errors) && result.errors.length);
  const error = ok
    ? null
    : discoveredValues.length === 0
      ? "no statement download buttons discovered after filters"
      : graphqlError
      ? `statement download GraphQL failed: ${graphqlError.operation_name || 'unknown'} ${graphqlError.errors[0].message}`
      : "no new PDF files were downloaded";
  const report = {
    ok,
    checked_at: new Date().toISOString(),
    target_year: TARGET_YEAR || null,
    target_month: TARGET_MONTH || null,
    clicked_load_more: clickedLoadMore,
    total_buttons: discoveredValues.length,
    discovery_href: discovery.href || null,
    discovery_body_sample: discovery.body_sample || null,
    discovery_button_texts: Array.isArray(discovery.button_texts) ? discovery.button_texts.slice(0, 80) : [],
    max_downloads: MAX_DOWNLOADS,
    clicked_buttons: clickedButtons,
    before_count: before.size,
    after_count: after.length,
    new_files_count: newFiles.length,
    new_files: newFiles.slice(0, 300),
    download_dir: DOWNLOAD_DIR,
    browser_download_dir: browserDownloadDir,
    click_results: clickResults.slice(0, 50),
    network_events: networkEvents.slice(-50),
    download_events: downloadEvents.slice(-50),
    graphql_click_results: graphqlClickResults.slice(-50),
    error,
    failure_html: ok ? null : htmlDumpPath,
    failure_screenshot: ok ? null : screenshotPath
  };
  fs.writeFileSync(reportPath, JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
  ws.close();
  if (!ok) process.exitCode = 1;
}

main().catch(async err => {
  fs.mkdirSync(REPORTS, { recursive: true });
  try { await _captureFailureArtifacts(); } catch {}
  const report = { ok: false, error: String(err && err.stack || err), checked_at: new Date().toISOString(), failure_html: htmlDumpPath, failure_screenshot: screenshotPath };
  fs.writeFileSync(reportPath, JSON.stringify(report, null, 2));
  console.error(report.error);
  process.exit(1);
});
