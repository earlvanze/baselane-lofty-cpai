#!/usr/bin/env node
/**
 * Split a Baselane mortgage transaction via UI automation using CDP.
 * Usage: node baselane_split_mortgage_via_ui.js <transaction_id> <splits_json_file>
 */
const fs = require('fs');

const versionUrl = 'http://localhost:9222/json/version';
const transactionId = process.argv[2];
const splitsFile = process.argv[3];

if (!transactionId || !splitsFile) {
  console.error('Usage: baselane_split_mortgage_via_ui.js <transaction_id> <splits_json_file>');
  process.exit(2);
}

const splits = JSON.parse(fs.readFileSync(splitsFile, 'utf8'));

async function main() {
  const version = await (await fetch(versionUrl)).json();
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

  ws.onmessage = ev => {
    const msg = JSON.parse(ev.data);
    if (msg.id) {
      const p = pending.get(msg.id);
      if (p) {
        pending.delete(msg.id);
        if (msg.error) p.reject(msg.error); else p.resolve(msg.result);
      }
    } else if (msg.method === 'Target.attachedToTarget') {
      sessions.set(msg.params.targetInfo.targetId, msg.params.sessionId);
    }
  };

  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
  await send('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: false, flatten: true });

  const targets = await send('Target.getTargets', {});
  const tab = (targets.targetInfos || []).find(t => t.type === 'page' && t.url && t.url.includes('app.baselane.com'));
  if (!tab) throw new Error('No Baselane tab found');

  const attached = await send('Target.attachToTarget', { targetId: tab.targetId, flatten: true });
  const sessionId = attached.sessionId;

  await send('Runtime.enable', {}, sessionId);
  await send('Page.enable', {}, sessionId);


  async function evalJS(expr, awaitPromise = true) {
    const res = await send('Runtime.evaluate', { expression: expr, awaitPromise, returnByValue: true }, sessionId);
    return res.result ? res.result.value : undefined;
  }

  async function waitFor(predicate, timeoutMs = 30000) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      const result = await evalJS(predicate);
      if (result) return result;
      await new Promise(r => setTimeout(r, 500));
    }
    throw new Error('Timeout waiting for condition');
  }

  // Navigate to transactions page with search for the transaction ID
  const searchUrl = `https://app.baselane.com/transactions?search=${transactionId}`;
  console.error(`[UI] Navigating to ${searchUrl}`);
  await send('Page.navigate', { url: searchUrl }, sessionId);
  await new Promise(r => setTimeout(r, 3000));

  // Wait for transaction row to appear
  await waitFor(`document.querySelector('[data-transaction-id="${transactionId}"]') !== null`);

  // Click on the transaction row to open detail panel
  console.error('[UI] Clicking transaction row');
  await evalJS(`document.querySelector('[data-transaction-id="${transactionId}"]').click()`);
  await new Promise(r => setTimeout(r, 2000));

  // Look for "Split" button and click it
  console.error('[UI] Looking for Split button');
  await waitFor(`Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes('Split')) !== undefined`);
  await evalJS(`Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes('Split')).click()`);
  await new Promise(r => setTimeout(r, 2000));

  // Fill in splits
  console.error(`[UI] Filling in ${splits.length} split components`);
  for (let i = 0; i < splits.length; i++) {
    const split = splits[i];
    console.error(`[UI]   Split ${i+1}: ${split.amount} - ${split.category}`);

    // Add split row if needed
    if (i > 0) {
      await evalJS(`Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes('Add split')).click()`);
      await new Promise(r => setTimeout(r, 1000));
    }

    // Fill amount (use selector pattern from existing splits)
    await evalJS(`
      const inputs = document.querySelectorAll('input[type="number"], input[placeholder*="amount" i]');
      inputs[${i}].value = '${Math.abs(split.amount)}';
      inputs[${i}].dispatchEvent(new Event('input', { bubbles: true }));
    `);

    // Select category dropdown
    await evalJS(`
      const selects = document.querySelectorAll('select, [role="combobox"]');
      const categorySelect = selects[${i}];
      categorySelect.click();
    `);
    await new Promise(r => setTimeout(r, 500));

    // Find and click the category option
    await evalJS(`
      const options = Array.from(document.querySelectorAll('[role="option"], option'));
      const target = options.find(o => o.textContent.includes('${split.category}'));
      if (target) target.click();
    `);
    await new Promise(r => setTimeout(r, 500));

    // Fill note if present
    if (split.note) {
      await evalJS(`
        const noteInputs = document.querySelectorAll('input[placeholder*="note" i], textarea');
        if (noteInputs[${i}]) {
          noteInputs[${i}].value = '${split.note}';
          noteInputs[${i}].dispatchEvent(new Event('input', { bubbles: true }));
        }
      `);
    }
  }

  // Click Save button
  console.error('[UI] Saving split');
  await evalJS(`Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes('Save')).click()`);
  await new Promise(r => setTimeout(r, 3000));

  // Verify split was created
  const verification = await evalJS(`
    fetch('https://orchestration.baselane.com/graphql', {
      method: 'POST',
      credentials: 'include',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        query: 'query { transaction(id: "${transactionId}") { id isSplit } }'
      })
    }).then(r => r.json())
  `);

  console.log(JSON.stringify({ success: true, transactionId, splits: splits.length, verification }, null, 2));
  ws.close();
}

main().catch(err => {
  console.error('ERROR:', err.message || String(err));
  process.exit(1);
});
