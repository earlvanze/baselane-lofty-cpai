#!/usr/bin/env node
/**
 * Single-shot Baselane split executor.
 * Re-auths, captures appcheck, fires createOrUpdateSplitTx mutation.
 * Usage: node baselane_do_split.js <parentTxnId> <splitsJson>
 *
 * splitsJson: JSON array of {amount, tagId, propertyId, merchantName, date}
 * Example: '[{"amount":-138.37,"tagId":"20","propertyId":31525,"merchantName":"P+I","date":"2026-01-07"}]'
 */
const FS = require('fs');
const PATH = require('path');

const VERSION_URL = process.env.BASELANE_CDP_VERSION_URL || 'http://127.0.0.1:9222/json/version';
const GRAPHQL_URL = process.env.BASELANE_GRAPHQL_URL || 'https://orchestration.baselane.com/graphql';
const CDP_COMMAND_TIMEOUT_MS = Number(process.env.BASELANE_NATIVE_SPLIT_CDP_COMMAND_TIMEOUT_MS || 30000);
const APP_CHECK_WAIT_MS = Number(process.env.BASELANE_NATIVE_SPLIT_APP_CHECK_WAIT_MS || 10000);

const parentTxnId = process.argv[2];
const splitsRaw = process.argv[3] || '[]';
const splits = JSON.parse(splitsRaw);

if (!parentTxnId) {
  console.error('Usage: node baselane_do_split.js <parentTxnId> <splitsJson>');
  console.error('Example: node baselane_do_split.js 251858224 \'[{"amount":-138.37,"tagId":"20","propertyId":31525,"merchantName":"P","date":"2026-03-10"}]\'');
  process.exit(1);
}
if (!splits.length) {
  console.error('No splits provided');
  process.exit(1);
}

async function main() {
  const version = await (await fetch(VERSION_URL)).json();
  const ws = new WebSocket(version.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();

  ws.onmessage = ev => {
    const msg = JSON.parse(ev.data);
    if (msg.id) { const p = pending.get(msg.id); if (p) { pending.delete(msg.id); msg.error ? p.reject(msg.error) : p.resolve(msg.result); } }
  };

  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  await send('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: false, flatten: true });

  function send(method, params = {}, sessionId) {
    const msg = { id: ++id, method, params };
    if (sessionId) msg.sessionId = sessionId;
    ws.send(JSON.stringify(msg));
    return new Promise((res, rej) => {
      const msgId = msg.id;
      const t = setTimeout(() => { pending.delete(msgId); rej(new Error(`timeout: ${method}`)); }, CDP_COMMAND_TIMEOUT_MS);
      pending.set(msgId, { resolve: v => { clearTimeout(t); res(v); }, reject: e => { clearTimeout(t); rej(e); } });
    });
  }

  async function attachToBaselaneTab(options = {}) {
    const targets = await send('Target.getTargets');
    let tab = targets.targetInfos.find(t => t.type === 'page' && t.url && t.url.includes('app.baselane.com') && !t.url.includes('/login'));
    let createdFresh = false;
    if (!tab && options.createIfMissing) {
      const created = await send('Target.createTarget', { url: options.url || 'https://app.baselane.com/transactions' });
      tab = { targetId: created.targetId, url: options.url || 'https://app.baselane.com/transactions' };
      createdFresh = true;
      await new Promise(r => setTimeout(r, 1000));
    }
    if (!tab) throw new Error('No Baselane tab found');

    async function attachAndEnable(targetInfo) {
      const attached = await send('Target.attachToTarget', { targetId: targetInfo.targetId, flatten: true });
      const targetSessionId = attached.sessionId;
      await send('Runtime.enable', {}, targetSessionId);
      if (options.network) await send('Network.enable', {}, targetSessionId);
      if (options.page) await send('Page.enable', {}, targetSessionId);
      return targetSessionId;
    }

    try {
      return await attachAndEnable(tab);
    } catch (err) {
      if (!options.recoverStale || createdFresh) throw err;
      console.error(`[0b] Baselane tab attach failed (${cdpErrorMessage(err)}); creating a fresh transactions tab...`);
      try { await send('Target.closeTarget', { targetId: tab.targetId }); } catch (_e) {}
      const created = await send('Target.createTarget', { url: options.url || 'https://app.baselane.com/transactions' });
      await new Promise(r => setTimeout(r, 1000));
      return await attachAndEnable({ targetId: created.targetId, url: options.url || 'https://app.baselane.com/transactions' });
    }
  }

  function cdpErrorMessage(err) {
    if (!err) return '';
    if (typeof err === 'string') return err;
    if (err.message) return String(err.message);
    try { return JSON.stringify(err); } catch (_e) { return String(err); }
  }

  // 1. Find and attach to the Baselane tab
  let sessionId = await attachToBaselaneTab({ network: true, page: true, createIfMissing: true, recoverStale: true, url: 'https://app.baselane.com/transactions' });

  // 2. Capture appcheck token from network traffic
  let appCheckToken = null;
  const origOnMsg = ws.onmessage;
  ws.onmessage = ev => {
    const msg = JSON.parse(ev.data);
    if (msg.id) { const p = pending.get(msg.id); if (p) { pending.delete(msg.id); msg.error ? p.reject(msg.error) : p.resolve(msg.result); } return; }
    // Capture appcheck from ExtraInfo
    if (msg.method === 'Network.requestWillBeSentExtraInfo') {
      const headers = msg.params.headers || {};
      const host = headers[':authority'] || headers['host'] || headers['Host'] || '';
      if (host.includes('orchestration.baselane.com')) {
        const v = headers['x-firebase-appcheck'] || headers['X-Firebase-AppCheck'];
        if (v) { appCheckToken = v; }
      }
    }
  };

  async function triggerGraphqlTraffic() {
    await send('Runtime.evaluate', {
      expression: `
        (async () => {
          try {
            await fetch('${GRAPHQL_URL}', {
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
      `,
      awaitPromise: true,
      returnByValue: true
    }, sessionId).catch(() => {});
  }

  // 3. Trigger network traffic to capture appcheck
  console.error('[1] Capturing appcheck token...');
  await send('Page.reload', {}, sessionId).catch(() => {});

  const waitForAppCheck = async () => {
    const attempts = Math.max(1, Math.ceil(APP_CHECK_WAIT_MS / 250));
    for (let i = 0; i < attempts && !appCheckToken; i++) {
      await new Promise(r => setTimeout(r, 250));
    }
  };

  await waitForAppCheck();
  if (!appCheckToken) {
    console.error('[1b] No token from reload; triggering GraphQL traffic...');
    await triggerGraphqlTraffic();
    await waitForAppCheck();
  }
  if (!appCheckToken) {
    console.error('[1c] No token from fetch; reloading transactions page...');
    await send('Page.navigate', { url: 'https://app.baselane.com/transactions' }, sessionId).catch(() => {});
    await new Promise(r => setTimeout(r, Math.min(3000, APP_CHECK_WAIT_MS)));
    await triggerGraphqlTraffic();
    await waitForAppCheck();
  }
  if (!appCheckToken) {
    throw new Error(`Could not capture appcheck token after ${APP_CHECK_WAIT_MS}ms waits`);
  }
  console.error('[2] Got appcheck token');

  // Re-read the parent immediately before mutation. The planned export can be
  // minutes old, and splitting a newly posted, pending, or already-split row
  // would corrupt the canonical source ledger.
  const parentQuery = `query NativeSplitParentPreflight($id: ID!) {
    transactionById(id: $id) {
      id amount pending isSplit isDeleted
    }
  }`;
  const parentPreflightScript = `
    (async () => {
      const resp = await fetch('${GRAPHQL_URL}', {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
          'x-firebase-appcheck': '${appCheckToken}'
        },
        body: JSON.stringify(${JSON.stringify({
          query: parentQuery,
          variables: {id: String(parentTxnId)}
        })})
      });
      return await resp.text();
    })()
  `;
  const parentPreflightResult = await send('Runtime.evaluate', {
    expression: parentPreflightScript,
    awaitPromise: true,
    returnByValue: true
  }, sessionId);
  const parentPreflightText = parentPreflightResult?.result?.value || '';
  let parentPreflight;
  try {
    parentPreflight = JSON.parse(parentPreflightText);
  } catch (_err) {
    throw new Error(`Native split parent preflight returned invalid JSON for ${parentTxnId}`);
  }
  if (parentPreflight.errors?.length) {
    throw new Error(`Native split parent preflight failed for ${parentTxnId}: ${parentPreflight.errors[0]?.message || 'GraphQL error'}`);
  }
  const parent = parentPreflight.data?.transactionById;
  const splitTotal = splits.reduce((total, split) => total + Number(split.amount || 0), 0);
  const parentAmount = Number(parent?.amount);
  if (!parent || String(parent.id) !== String(parentTxnId)) {
    throw new Error(`Native split parent ${parentTxnId} was not found during immediate preflight`);
  }
  if (parent.pending === true) {
    throw new Error(`Transaction ${parentTxnId} is pending and cannot be split`);
  }
  if (parent.isDeleted === true || parent.isSplit === true) {
    throw new Error(`Transaction ${parentTxnId} is already deleted or split and cannot be safely mutated`);
  }
  if (!Number.isFinite(parentAmount) || Math.abs(parentAmount - splitTotal) > 0.00001) {
    throw new Error(`Native split total ${splitTotal.toFixed(2)} does not match live parent ${parentTxnId} amount ${String(parent?.amount)}`);
  }
  console.error(`[2b] Parent ${parentTxnId} verified unsplit, posted, and amount-matched`);

  // 4. Build and execute the mutation
  const mutation = `mutation createOrUpdateSplitTx($parentTransactionId: ID!, $splitType: SplitType!, $transactionSplitInputs: [TransactionSplitInput!]!) {
    createOrUpdateSplitTx(
      input: {parentTransactionId: $parentTransactionId, transactionSplitInputs: $transactionSplitInputs, splitType: $splitType}
    ) {
      id
      splitTransactions {
        id
        tagId
        amount
        merchantName
        date
      }
    }
  }`;

  const variables = {
    parentTransactionId: String(parentTxnId),
    splitType: 'AMOUNT',
    transactionSplitInputs: splits.map(s => {
      const propertyId = s.propertyId === null || s.propertyId === undefined || String(s.propertyId).trim() === ''
        ? null
        : String(s.propertyId);
      return {
        tagId: String(s.tagId),
        propertyId,
        date: String(s.date),
        amount: parseFloat(s.amount),
        merchantName: String(s.merchantName),
        propertyUnitId: null
      };
    })
  };

  console.error(`[3] Executing split on txn ${parentTxnId} with ${splits.length} components...`);

  // 5. Execute mutation via fetch inside page context
  const gqlScript = `
    (async () => {
      const resp = await fetch('${GRAPHQL_URL}', {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
          'x-firebase-appcheck': '${appCheckToken}'
        },
        body: JSON.stringify(${JSON.stringify({query: mutation, variables})})
      });
      return await resp.text();
    })()
  `;

  async function executeMutation(targetSessionId) {
    return await send('Runtime.evaluate', {
      expression: gqlScript,
      awaitPromise: true,
      returnByValue: true
    }, targetSessionId);
  }

  let gqlResult;
  try {
    gqlResult = await executeMutation(sessionId);
  } catch (err) {
    const message = cdpErrorMessage(err);
    if (/navigated|closed|Session|Cannot find context/i.test(message)) {
      console.error(`[3b] Mutation target became unstable (${message}); reattaching and retrying once...`);
      sessionId = await attachToBaselaneTab({ createIfMissing: true, recoverStale: true, url: 'https://app.baselane.com/transactions' });
      gqlResult = await executeMutation(sessionId);
    } else {
      throw err;
    }
  }

  const gqlText = gqlResult.result?.value || gqlResult.result;
  console.error(`[4] Response: ${gqlText.substring(0, 500)}`);

  ws.close();

  // Parse and report
  try {
    const parsed = JSON.parse(gqlText);
    if (parsed.data?.createOrUpdateSplitTx) {
      const result = parsed.data.createOrUpdateSplitTx;
      console.log(JSON.stringify({
        success: true,
        parentId: result.id,
        children: result.splitTransactions.map(t => ({
          id: t.id,
          tagId: t.tagId,
          amount: t.amount,
          merchantName: t.merchantName
        }))
      }, null, 2));
    } else if (parsed.errors) {
      console.error('GraphQL Errors:', JSON.stringify(parsed.errors, null, 2));
      process.exit(1);
    }
  } catch(e) {
    console.error('Parse error:', e.message, gqlText.substring(0, 200));
    process.exit(1);
  }
}

main().catch(err => { console.error('FATAL:', err.message); process.exit(1); });
