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
const CLICK_DELAY_MS = Number(process.env.BASELANE_CLICK_DELAY_MS || 1500);
const FINAL_WAIT_MS = Number(process.env.BASELANE_FINAL_WAIT_MS || 15000);
const reportPath = path.join(REPORTS, 'baselane_statements_download_report.json');
const htmlDumpPath = path.join(REPORTS, 'baselane_statements_failure.html');
const screenshotPath = path.join(REPORTS, 'baselane_statements_failure.png');

function getCred(field) {
  return execFileSync('bash', [path.join(ROOT, 'scripts', 'bw_get_baselane.sh'), field], { encoding: 'utf8' }).trim();
}

let _captureFailureArtifacts = async () => {};

async function main() {
  fs.mkdirSync(REPORTS, { recursive: true });
  fs.mkdirSync(DOWNLOAD_DIR, { recursive: true });
  const email = getCred('username');
  const password = getCred('password');
  const before = new Set(fs.readdirSync(DOWNLOAD_DIR).filter(f => f.endsWith('.pdf')));

  const version = await (await fetch(VERSION_URL)).json();
  const ws = new WebSocket(version.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  const sessions = new Map();

  function send(method, params = {}, sessionId) {
    const msg = { id: ++id, method, params };
    if (sessionId) msg.sessionId = sessionId;
    ws.send(JSON.stringify(msg));
    return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
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
  };

  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
  await send('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: false, flatten: true });

  const targets = await send('Target.getTargets', {});
  for (const t of (targets.targetInfos || [])) {
    if (t.type === 'page' && t.url && t.url.includes('app.baselane.com/login/something-went-wrong')) {
      try { await send('Target.closeTarget', { targetId: t.targetId }); } catch {}
    }
  }

  const { targetId } = await send('Target.createTarget', { url: 'https://app.baselane.com/login' });
  let sessionId = null;
  for (let i = 0; i < 120; i++) {
    sessionId = sessions.get(targetId);
    if (sessionId) break;
    await new Promise(r => setTimeout(r, 100));
  }
  if (!sessionId) throw new Error('no session attached');

  await send('Page.enable', {}, sessionId);
  await send('Runtime.enable', {}, sessionId);
  await send('Browser.setDownloadBehavior', { behavior: 'allow', downloadPath: DOWNLOAD_DIR }, sessionId);

  async function evalExpr(expression, awaitPromise = true) {
    const res = await send('Runtime.evaluate', { expression, awaitPromise, returnByValue: true }, sessionId);
    return res.result ? res.result.value : undefined;
  }

  async function captureFailureArtifacts() {
    try {
      const html = await evalExpr('document.documentElement ? document.documentElement.outerHTML : ');
      if (html) fs.writeFileSync(htmlDumpPath, html, 'utf8');
    } catch {}
    try {
      const shot = await send('Page.captureScreenshot', { format: 'png' }, sessionId);
      if (shot && shot.data) fs.writeFileSync(screenshotPath, Buffer.from(shot.data, 'base64'));
    } catch {}
  }

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

  await send('Page.navigate', { url: 'https://app.baselane.com/banking/statements' }, sessionId);
  for (let i = 0; i < 120; i++) {
    const s = await evalExpr(`({href:location.href,body:(document.body?.innerText||'').slice(0,1000)})`);
    if (String(s.href).includes('/login')) throw new Error('fell back to login on statements');
    if (String(s.href).includes('/banking/statements') && /Account Name[\s\S]*Statement Period[\s\S]*Download/.test(s.body || '')) break;
    await new Promise(r => setTimeout(r, 1000));
  }

  if (TARGET_YEAR) {
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

  const result = await evalExpr(`(async () => {
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    let clickedLoadMore = 0;
    for (let i = 0; i < ${MAX_LOAD_MORE}; i++) {
      const loadMore = Array.from(document.querySelectorAll('button')).find(b => /load more/i.test((b.innerText||b.textContent||'').trim()));
      if (!loadMore) break;
      loadMore.click();
      clickedLoadMore++;
      await sleep(1800);
    }
    const values = Array.from(document.querySelectorAll('button[value]')).filter(b => (b.value||'').includes(',') && b.querySelector('svg')).map(b => b.value);
    for (const value of values) {
      const btn = Array.from(document.querySelectorAll('button[value]')).filter(b => (b.value||'').includes(',') && b.querySelector('svg')).find(b => b.value === value);
      if (btn) btn.click();
      await sleep(${CLICK_DELAY_MS});
    }
    return { clickedLoadMore, totalButtons: values.length };
  })()`);

  await new Promise(r => setTimeout(r, FINAL_WAIT_MS));
  const after = fs.readdirSync(DOWNLOAD_DIR).filter(f => f.endsWith('.pdf'));
  const newFiles = after.filter(f => !before.has(f));
  const report = {
    ok: true,
    checked_at: new Date().toISOString(),
    target_year: TARGET_YEAR || null,
    target_month: TARGET_MONTH || null,
    clicked_load_more: result.clickedLoadMore,
    total_buttons: result.totalButtons,
    before_count: before.size,
    after_count: after.length,
    new_files_count: newFiles.length,
    new_files: newFiles.slice(0, 300)
  };
  fs.writeFileSync(reportPath, JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
  ws.close();
}

main().catch(async err => {
  fs.mkdirSync(REPORTS, { recursive: true });
  try { await _captureFailureArtifacts(); } catch {}
  const report = { ok: false, error: String(err && err.stack || err), checked_at: new Date().toISOString(), failure_html: htmlDumpPath, failure_screenshot: screenshotPath };
  fs.writeFileSync(reportPath, JSON.stringify(report, null, 2));
  console.error(report.error);
  process.exit(1);
});
