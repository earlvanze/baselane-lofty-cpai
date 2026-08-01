#!/usr/bin/env node
/**
 * baselane_export_human_paced.js
 *
 * Authenticated browser-session export:
 * - Assumes a human has already authenticated the visible Baselane browser
 * - Uses a deterministic, configurable throttle between API calls
 * - Triggers GraphQL to capture fresh appcheck token
 * - Keeps one CDP attachment for the complete paginated export
 */
const fs = require('fs');
const path = require('path');
// Use ws package for custom header support (needed for Docker→Windows CDP)
let WebSocketImpl;
try { WebSocketImpl = require('ws'); }
catch (_e) { WebSocketImpl = WebSocket; }

const OPENCLAW_ROOT = process.env.OPENCLAW_ROOT || path.join(process.env.HOME || '', '.openclaw');
const ROOT = process.env.WORKSPACE_ROOT || path.join(OPENCLAW_ROOT, 'workspace');
const REPORTS = path.join(ROOT, 'reports');
const DROPBOX_ROOT = process.env.DROPBOX_ROOT || path.join(ROOT, 'Dropbox');
const TRACKER = process.env.BASELANE_LEDGER_DIR || path.join(DROPBOX_ROOT, 'Projects/assetrail');
const OUT_PATH = process.env.BASELANE_LEDGER_PATH || path.join(TRACKER, 'ECO Systems General Ledger.csv');
const versionUrl = process.env.BASELANE_CDP_VERSION_URL || 'http://[::1]:9222/json/version';
const maxPages = Number(process.env.BASELANE_MAX_PAGES || 200);
const pageLimit = Number(process.env.BASELANE_PAGE_LIMIT || 200);
const MIN_ROWS = Number(process.env.BASELANE_MIN_ROWS || 6000);
const MAX_ROWS = Number(process.env.BASELANE_MAX_ROWS || 25000);
const commandTimeoutMs = Math.max(1000, Number(process.env.BASELANE_EXPORT_CDP_COMMAND_TIMEOUT_MS || 30000));
const fetchTimeoutMs = Math.max(1000, Number(process.env.BASELANE_EXPORT_FETCH_TIMEOUT_MS || 20000));

const requestDelayMs = Math.max(0, Number(process.env.BASELANE_EXPORT_REQUEST_DELAY_MS || 250));
const authPollDelayMs = Math.max(100, Number(process.env.BASELANE_EXPORT_AUTH_POLL_DELAY_MS || 250));
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const requestDelay = () => delay(requestDelayMs);
const authPollDelay = () => delay(authPollDelayMs);

const EXCLUDE_RAW = [
  '3880 Dover St.', '3880 Dover St', 'Crypto Investments',
  'Dome', 'EVCO Holdings', 'Mining, Sales, Consulting, and PM',
  'Mining, Sales, Consulting, & PM', 'NARWALL Holdings', 'Personal', 'Vehicles',
];
function normalizeName(v) {
  return String(v || '').trim().toLowerCase().replaceAll('&', ' and ').replace(/[^a-z0-9]+/g, ' ').replace(/\s+/g, ' ').trim();
}
const EXCLUDE_NORM = new Set(EXCLUDE_RAW.map(normalizeName));
const ECO_ACCRUAL_NOTE = /^AOPS-(?:(?:MONTHLY|OHIL|PAU|PNL)-ACCRUAL|PM-FEE)\|(dao_eco|pm_eco)\|([^|]+)\|(\d{4}-\d{2})\|(-?\d+(?:\.\d{1,2})?)(?:\s|\||$)/;

function ecoAccrualTargetProperty(row) {
  const marker = String(row.Notes || '').match(ECO_ACCRUAL_NOTE);
  if (!marker) return '';
  const amount = Number(row.Amount);
  const markerAmount = Number(marker[4]);
  const merchant = String(row.Merchant || '').trim();
  const expectedPrefix = marker[1] === 'dao_eco'
    ? 'ECO Systems LLC DAO Registration Fee Revenue | '
    : 'ECO Systems LLC PM Fee Revenue | ';
  if (
    !Number.isFinite(amount)
    || !Number.isFinite(markerAmount)
    || Math.abs(amount - markerAmount) > 0.001
    || String(row.Type || '').trim() !== 'Revenue'
    || String(row.Category || '').trim() !== 'Fees & Other Revenue'
    || !merchant.startsWith(expectedPrefix)
  ) return '';
  return marker[2].trim();
}

function csvEscape(v) {
  if (v === null || v === undefined) return '';
  const s = String(v);
  return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

function noteText(value) {
  return value && typeof value === 'object' ? String(value.text || '') : String(value || '');
}

async function main() {
  fs.mkdirSync(REPORTS, {recursive: true});
  fs.mkdirSync(path.dirname(OUT_PATH), {recursive: true});

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
  let lastAppCheck = null;

  function send(method, params = {}, sessionId) {
    const msg = {id: ++id, method, params};
    if (sessionId) msg.sessionId = sessionId;
    ws.send(JSON.stringify(msg));
    const requestId = id;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(requestId);
        reject(new Error(`CDP ${method} timed out after ${commandTimeoutMs}ms`));
      }, commandTimeoutMs);
      pending.set(requestId, {
        method,
        resolve: value => { clearTimeout(timer); resolve(value); },
        reject: error => { clearTimeout(timer); reject(error); },
      });
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
        if (msg.error) p.reject(msg.error); else p.resolve(msg.result);
      }
      return;
    }
    if (msg.method === 'Target.attachedToTarget') {
      sessions.set(msg.params.targetInfo.targetId, msg.params.sessionId);
    }
    // Capture appcheck from any Baselane request
    if (msg.sessionId && msg.method === 'Network.requestWillBeSentExtraInfo') {
      const h = msg.params.headers || {};
      const authority = h[':authority'] || h['host'] || h['Host'] || '';
      if (String(authority).includes('orchestration.baselane.com') || String(authority).includes('baselane.com')) {
        const v = h['x-firebase-appcheck'] || h['X-Firebase-AppCheck'];
        if (v) lastAppCheck = v;
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

  // Find existing authenticated Baselane tab
  const targets = await send('Target.getTargets', {});
  const existing = (targets.targetInfos || []).find(t =>
    t.type === 'page' && t.url && t.url.includes('app.baselane.com') && !t.url.includes('/login')
  );

  if (!existing) {
    throw new Error('No authenticated Baselane tab found. Sign in through the visible browser, then rerun.');
  }

  console.log('[CDP] Found existing Baselane tab:', existing.url.substring(0, 60));
  const attached = await send('Target.attachToTarget', {targetId: existing.targetId, flatten: true});
  const sessionId = attached.sessionId;

  await send('Network.enable', {}, sessionId);
  await send('Runtime.enable', {}, sessionId);
  await send('Page.enable', {}, sessionId);

  async function evalExpr(expression, awaitPromise = true) {
    const res = await send('Runtime.evaluate', {expression, awaitPromise, returnByValue: true}, sessionId);
    return res.result ? res.result.value : undefined;
  }

  // TRIGGER: Make a fetch call to force appcheck generation
  console.log('[CDP] Triggering GraphQL call to capture appcheck token...');
  evalExpr(`
    (async () => {
      try {
        // Trigger a fetch that Baselane's app will make
        await fetch('https://orchestration.baselane.com/graphql', {
          method: 'POST',
          credentials: 'include',
          headers: {
            'accept': '*/*',
            'content-type': 'application/json'
          },
          body: JSON.stringify({
            operationName: 'PropertyList',
            variables: {},
            query: 'query PropertyList { property { id } }'
          })
        });
      } catch(e) {}
      return 'triggered';
    })()
  `, true).catch(() => {});

  // Wait for appcheck token to be captured
  console.log('[CDP] Waiting for x-firebase-appcheck token...');
  const appCheckStart = Date.now();
  while (!lastAppCheck && (Date.now() - appCheckStart) < 30000) {
    await authPollDelay();
  }

  if (!lastAppCheck) {
    // Try triggering with page navigation/reload as fallback
    console.log('[CDP] No appcheck yet, refreshing transactions page...');
    await send('Page.navigate', {url: 'https://app.baselane.com/transactions'}, sessionId);
    await delay(1000);

    // Try another trigger after navigation
    evalExpr(`
      (async () => {
        try {
          await fetch('https://orchestration.baselane.com/graphql', {
            method: 'POST',
            credentials: 'include',
            headers: {
              'accept': '*/*',
              'content-type': 'application/json'
            },
            body: JSON.stringify({
              operationName: 'PropertyList',
              variables: {},
              query: 'query PropertyList { property { id } }'
            })
          });
        } catch(e) {}
        return 'triggered2';
      })()
    `, true).catch(() => {});

    const waitStart = Date.now();
    while (!lastAppCheck && (Date.now() - waitStart) < 30000) {
      await authPollDelay();
    }
  }

  if (!lastAppCheck) {
    throw new Error('Could not capture x-firebase-appcheck token');
  }
  console.log('[CDP] Got appcheck token');

  async function gql(operationName, query, variables = {}) {
    await requestDelay();
    console.log(`[CDP] GraphQL ${operationName}...`);

    const payload = {operationName, variables, query};
    const outer = await evalExpr(`(async () => {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), ${fetchTimeoutMs});
      try {
        const resp = await fetch('https://orchestration.baselane.com/graphql', {
          method: 'POST', credentials: 'include', signal: controller.signal, headers: {
            'accept': '*/*',
            'content-type': 'application/json',
            'x-firebase-appcheck': ${JSON.stringify(lastAppCheck)}
          }, body: JSON.stringify(${JSON.stringify(payload)})
        });
        const text = await resp.text();
        return { ok: resp.ok, status: resp.status, text };
      } catch (err) {
        return { fetchError: true, errorMessage: err?.message || String(err) };
      } finally {
        clearTimeout(timer);
      }
    })()`);

    if (!outer || outer.fetchError || outer.status !== 200) {
      throw new Error(`GraphQL ${operationName} failed: ` + JSON.stringify(outer));
    }

    const parsed = JSON.parse(outer.text);
    if (parsed.errors) throw new Error(`GraphQL ${operationName} errors: ${JSON.stringify(parsed.errors)}`);
    return parsed.data || {};
  }

  // Get properties with delay
  console.log('[CDP] Fetching properties...');
  const props = (await gql('PropertyList', 'query PropertyList { property { id name address } }')).property || [];
  console.log(`[CDP] Found ${props.length} properties`);

  const propMap = Object.fromEntries(props.map(p => [String(p.id), p.name || '']));
  const excludedIds = new Set();
  for (const p of props) {
    if (EXCLUDE_NORM.has(normalizeName(p.name || ''))) excludedIds.add(String(p.id));
  }
  const selectedPropertyIds = new Set(props.filter(p => !excludedIds.has(String(p.id))).map(p => String(p.id)));
  console.log(`[CDP] Selected ${selectedPropertyIds.size} properties (excluded ${excludedIds.size})`);

  await requestDelay();

  // Get tags
  console.log('[CDP] Fetching tags...');
  const tags = (await gql('TagList', 'query TagList { tag { type subType { id name subType { id name subType { id name } } } } }')).tag || [];
  const tagMap = {};
  function mapTagSubTypes(subTypes, type) {
    for (const subType of (subTypes || [])) {
      tagMap[String(subType.id)] = [type || '', subType.name || ''];
      mapTagSubTypes(subType.subType || [], type);
    }
  }
  for (const tag of tags) mapTagSubTypes(tag.subType || [], tag.type || '');

  await requestDelay();

  // Fetch transactions with human-paced pagination
  console.log('[CDP] Fetching transactions...');
  const txQuery = `query Transactions($input: SortsAndFilters) {
    transactions(input: $input) {
      total
      data {
        description bankAccountId amount merchantName name pending time hidden isDeleted isExternal isManual isSplit parentId isReviewedByUser tagIdSource propertyTagIdSource tagRuleId propertyRuleId originalTransaction isDocumentUploaded linkedAssetId linkedLoanId id tagId date propertyId unitId note
      }
    }
  }`;

  const fields = ['Account','Date','Merchant','Description','Amount','Type','Category','Sub-category','Property','Unit','Notes'];
  const rows = [];
  let page = 1;
  let total = null;

  while (page < maxPages) {
    if (page > 1) await requestDelay();

    const input = {
      sort: {direction: 'DESC', field: 'date'},
      filter: {search: '', isHidden: false, isDeleted: false},
      page,
      pageLimit,
    };
    const result = await gql('Transactions', txQuery, {input});
    const batch = result.transactions?.data || [];
    total = result.transactions?.total ?? total;

    if (batch.length > pageLimit) {
      throw new Error(`GraphQL Transactions ignored pagination: received ${batch.length} rows for pageLimit ${pageLimit}`);
    }

    console.log(`[CDP] Page ${page}: ${batch.length} transactions (total: ${total})`);

    for (const tx of batch) {
      const pid = String(tx.propertyId || '');
      const tagPair = tagMap[String(tx.tagId || '')] || ['', ''];
      const row = {
        Amount: tx.amount || '',
        Merchant: tx.merchantName || '',
        Description: tx.description || tx.name || tx.merchantName || '',
        Type: tagPair[0],
        Category: tagPair[1],
        Notes: noteText(tx.note),
      };
      const excludedEcoTarget = ecoAccrualTargetProperty(row);
      if (!selectedPropertyIds.has(pid) && !excludedEcoTarget) continue;
      rows.push([
        excludedEcoTarget || propMap[pid] || pid,
        tx.date || '',
        row.Merchant,
        row.Description,
        row.Amount,
        tx.isSplit ? 'Split' : (tx.isManual ? 'Manual' : 'Transaction'),
        row.Type,
        row.Category,
        excludedEcoTarget || propMap[pid] || '',
        '', // Unit
        row.Notes
      ]);
    }

    if (batch.length < pageLimit) break;
    page++;
  }

  // Write CSV
  const csv = [fields.map(csvEscape).join(','), ...rows.map(r => r.map(csvEscape).join(','))].join('\n');
  fs.writeFileSync(OUT_PATH, csv);

  const report = {
    ok: true,
    rows: rows.length,
    total,
    pages: page + 1,
    output: OUT_PATH,
    checked_at: new Date().toISOString()
  };
  fs.writeFileSync(path.join(REPORTS, 'baselane_export_report.json'), JSON.stringify(report, null, 2));
  console.log(`[CDP] Done! Exported ${rows.length} rows to ${OUT_PATH}`);

  ws.close();
  process.exit(0);
}

main().catch(err => {
  fs.mkdirSync(REPORTS, {recursive: true});
  fs.writeFileSync(path.join(REPORTS, 'baselane_export_report.json'), JSON.stringify({
    ok: false, error: String(err?.stack || err), checked_at: new Date().toISOString()
  }, null, 2));
  console.error(err);
  process.exit(1);
});
