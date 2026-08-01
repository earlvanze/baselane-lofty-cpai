#!/usr/bin/env node

const http = require('http');
const WebSocket = require('ws');

const listUrl = new URL(process.env.LAWNSTARTER_CDP_LIST_URL || 'http://127.0.0.1:19222/json/list');
const timeoutMs = Number(process.env.LAWNSTARTER_CDP_TIMEOUT_MS || 60000);

function getJson(url) {
  return new Promise((resolve, reject) => {
    const req = http.get(url, response => {
      let body = '';
      response.on('data', chunk => { body += chunk; });
      response.on('end', () => {
        try { resolve(JSON.parse(body)); } catch (error) { reject(error); }
      });
    });
    req.setTimeout(timeoutMs, () => req.destroy(new Error('CDP list timeout')));
    req.on('error', reject);
  });
}

async function main() {
  const targets = await getJson(listUrl);
  const target = targets.find(item =>
    item.type === 'page' && String(item.url || '').startsWith('https://my.lawnstarter.com/')
  );
  if (!target || !target.webSocketDebuggerUrl) throw new Error('authenticated LawnStarter CDP tab not found');

  const ws = new WebSocket(target.webSocketDebuggerUrl);
  let sequence = 0;
  const pending = new Map();
  const timer = setTimeout(() => {
    ws.terminate();
    process.exitCode = 2;
  }, timeoutMs);

  ws.on('message', raw => {
    const message = JSON.parse(String(raw));
    const waiter = pending.get(message.id);
    if (!waiter) return;
    pending.delete(message.id);
    if (message.error) waiter.reject(new Error(JSON.stringify(message.error)));
    else waiter.resolve(message.result);
  });
  await new Promise((resolve, reject) => {
    ws.once('open', resolve);
    ws.once('error', reject);
  });
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++sequence;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });

  const expression = `(async () => {
    const token = localStorage.getItem('@LawnStarter:authToken');
    if (!token) throw new Error('LawnStarter auth token missing');
    const headers = {
      'accept': 'application/json',
      'x-auth-token': token,
      'x-ls-client': 'lawnstarter-customer-web',
      'x-ls-clientversionnumber': '6.240.0',
      'x-ls-os-name': 'web',
      'x-ls-os-version': '0.0.0'
    };
    const pages = [];
    for (let page = 1; page <= 100; page += 1) {
      const response = await fetch('https://api.lawnstarter.com/v2/customers/1494561/billing?page=' + page, { headers });
      if (!response.ok) throw new Error('LawnStarter billing HTTP ' + response.status);
      const payload = await response.json();
      pages.push(payload);
      const pagination = payload.pagination || {};
      const total = Number(pagination.totalItems || 0);
      const perPage = Number(pagination.itemsPerPage || 10);
      if (!total || page * perPage >= total) break;
    }
    return pages;
  })()`;
  const result = await send('Runtime.evaluate', {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  const value = result.result && result.result.value;
  if (!Array.isArray(value)) {
    const detail = result.exceptionDetails ? JSON.stringify(result.exceptionDetails) : 'no result';
    throw new Error(`LawnStarter billing fetch failed: ${detail}`);
  }
  process.stdout.write(JSON.stringify(value));
  clearTimeout(timer);
  ws.close();
}

main().catch(error => {
  console.error(error.message || String(error));
  process.exit(2);
});
