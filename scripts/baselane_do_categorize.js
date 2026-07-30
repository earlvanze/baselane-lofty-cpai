#!/usr/bin/env node
/**
 * Baselane Categorization Executor
 * Updates transaction Type and Category via GraphQL mutation
 * Usage: node baselane_do_categorize.js <baselaneId> <type> <category>
 */
const VERSION_URL = process.env.BASELANE_CDP_VERSION_URL || 'http://127.0.0.1:9222/json/version';
const GRAPHQL_URL = process.env.BASELANE_GRAPHQL_URL || 'https://orchestration.baselane.com/graphql';
const CDP_COMMAND_TIMEOUT_MS = Number(process.env.BASELANE_CATEGORIZE_CDP_TIMEOUT_MS || 30000);
const APP_CHECK_WAIT_MS = Number(process.env.BASELANE_CATEGORIZE_APP_CHECK_WAIT_MS || 10000);

const txnId = process.argv[2];
const newType = process.argv[3];
const newCategory = process.argv[4];

if (!txnId || !newType || !newCategory) {
  console.error('Usage: node baselane_do_categorize.js <baselaneId> <Type> <Category>');
  console.error('Example: node baselane_do_categorize.js 251858224 "Operating Expenses" "Supplies"');
  process.exit(1);
}

async function main() {
  const version = await (await fetch(VERSION_URL)).json();
  const ws = new WebSocket(version.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();

  ws.onmessage = ev => {
    const msg = JSON.parse(ev.data);
    if (msg.id) {
      const p = pending.get(msg.id);
      if (p) {
        pending.delete(msg.id);
        msg.error ? p.reject(msg.error) : p.resolve(msg.result);
      }
    }
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
      pending.set(msgId, {
        resolve: v => { clearTimeout(t); res(v); },
        reject: e => { clearTimeout(t); rej(e); }
      });
    });
  }

  async function attachToBaselaneTab() {
    const targets = await send('Target.getTargets');
    let tab = targets.targetInfos.find(t => t.type === 'page' && t.url && t.url.includes('app.baselane.com'));
    if (!tab) {
      const created = await send('Target.createTarget', { url: 'https://app.baselane.com/transactions' });
      tab = { targetId: created.targetId };
      await new Promise(r => setTimeout(r, 1000));
    }
    const attached = await send('Target.attachToTarget', { targetId: tab.targetId, flatten: true });
    const sessionId = attached.sessionId;
    await send('Runtime.enable', {}, sessionId);
    await send('Network.enable', {}, sessionId);
    return sessionId;
  }

  // Attach to Baselane tab
  let sessionId = await attachToBaselaneTab();

  // Capture appcheck token
  let appCheckToken = null;
  ws.onmessage = ev => {
    const msg = JSON.parse(ev.data);
    if (msg.id) {
      const p = pending.get(msg.id);
      if (p) { pending.delete(msg.id); msg.error ? p.reject(msg.error) : p.resolve(msg.result); }
      return;
    }
    if (msg.method === 'Network.requestWillBeSentExtraInfo') {
      const headers = msg.params.headers || {};
      const host = headers[':authority'] || headers['host'] || '';
      if (host.includes('orchestration.baselane.com')) {
        const v = headers['x-firebase-appcheck'] || headers['X-Firebase-AppCheck'];
        if (v) appCheckToken = v;
      }
    }
  };

  // Trigger traffic to capture appcheck
  await send('Page.reload', {}, sessionId).catch(() => {});
  for (let i = 0; i < 40 && !appCheckToken; i++) {
    await new Promise(r => setTimeout(r, 250));
  }

  if (!appCheckToken) {
    throw new Error('Could not capture appcheck token');
  }
  console.error('[1] Got appcheck token');

  // First get the tag mapping
  const tagQuery = `query TagList { tag { type subType { id name } } }`;
  const tagScript = `
    (async () => {
      const resp = await fetch('${GRAPHQL_URL}', {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
          'x-firebase-appcheck': '${appCheckToken}'
        },
        body: JSON.stringify({query: ${JSON.stringify(tagQuery)}, variables: {}})
      });
      return await resp.text();
    })()
  `;

  const tagResult = await send('Runtime.evaluate', {
    expression: tagScript,
    awaitPromise: true,
    returnByValue: true
  }, sessionId);

  // Find the correct tagId for the category
  const tagData = JSON.parse(tagResult.result?.value || '{}');
  const tagMap = {};
  if (tagData.data?.tag) {
    for (const tag of tagData.data.tag) {
      for (const subType of tag.subType || []) {
        if (subType.name === newCategory && tag.type === newType) {
          tagMap[newCategory] = subType.id;
        }
      }
    }
  }

  const tagId = tagMap[newCategory];
  if (!tagId) {
    console.error(`Could not find tagId for ${newType} / ${newCategory}`);
    process.exit(1);
  }
  console.error(`[2] Found tagId ${tagId} for ${newType} / ${newCategory}`);

  // Execute categorization mutation
  const mutation = `mutation updateTransactionTag($transactionId: ID!, $tagId: ID!) {
    updateTransactionTag(input: {transactionId: $transactionId, tagId: $tagId}) {
      id
      tagId
      tag { type subType { name } }
    }
  }`;

  const variables = {
    transactionId: String(txnId),
    tagId: String(tagId)
  };

  console.error(`[3] Updating transaction ${txnId} to ${newType} / ${newCategory}...`);

  const gqlScript = `
    (async () => {
      const resp = await fetch('${GRAPHQL_URL}', {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
          'x-firebase-appcheck': '${appCheckToken}'
        },
        body: JSON.stringify({query: ${JSON.stringify(mutation)}, variables: ${JSON.stringify(variables)}})
      });
      return await resp.text();
    })()
  `;

  const result = await send('Runtime.evaluate', {
    expression: gqlScript,
    awaitPromise: true,
    returnByValue: true
  }, sessionId);

  const response = result.result?.value || result.result;
  console.log(JSON.stringify({
    transactionId: txnId,
    type: newType,
    category: newCategory,
    tagId: tagId,
    response: response.substring(0, 500)
  }));

  ws.close();
  process.exit(0);
}

main().catch(err => {
  console.error('Error:', err.message || err);
  process.exit(1);
});
