#!/usr/bin/env node
const CDP = require('chrome-remote-interface');
const fs = require('fs');
const path = require('path');
const https = require('https');
const http = require('http');

const DOWNLOAD_DIR = process.env.DOWNLOAD_DIR || path.join(process.env.HOME, '.openclaw/workspace/baselane-statements');
const MAX_DOWNLOADS = Number(process.env.MAX_DOWNLOADS) || 200;

async function main() {
  fs.mkdirSync(DOWNLOAD_DIR, {recursive: true});

  const targets = await CDP.List();
  const baselaneTarget = targets.find(t => t.url.includes('baselane.com'));

  if (!baselaneTarget) {
    console.error('No Baselane tab found');
    process.exit(1);
  }

  const client = await CDP({target: baselaneTarget.id});
  const {Page, Runtime, Network, Browser} = client;

  await Page.enable();
  await Network.enable();

  // Track downloads
  let downloadedCount = 0;
  const downloaded = [];

  // Set up download behavior
  await Browser.setDownloadBehavior({
    behavior: 'allow',
    downloadPath: DOWNLOAD_DIR
  });

  // Navigate to statements
  console.log('[CDP] Navigating to statements page...');
  await Page.navigate({url: 'https://app.baselane.com/banking/statements'});
  await new Promise(r => setTimeout(r, 5000));

  // Scroll to load all
  console.log('[CDP] Loading all statements...');
  for (let i = 0; i < 15; i++) {
    await Runtime.evaluate({expression: `window.scrollTo(0, ${(i+1) * 800})`});
    await new Promise(r => setTimeout(r, 400));
  }
  await Runtime.evaluate({expression: 'window.scrollTo(0, 0)'});
  await new Promise(r => setTimeout(r, 2000));

  // Get all download buttons
  const buttons = await Runtime.evaluate({
    expression: `
      Array.from(document.querySelectorAll('button[value]')).filter(b =>
        b.value.includes(',') && b.querySelector('svg')
      ).map((b, idx) => {
        const row = b.closest('.chakra-stack');
        const texts = Array.from(row?.querySelectorAll('p') || []).map(p => p.textContent?.trim());
        return {
          index: idx,
          value: b.value,
          account: texts[0],
          subaccount: texts[1],
          period: texts[2] || texts[texts.length - 1]
        };
      })
    `,
    returnByValue: true
  });

  const statements = buttons.result.value;
  console.log(`[CDP] Found ${statements.length} statements`);

  // Download each
  for (let i = 0; i < Math.min(statements.length, MAX_DOWNLOADS); i++) {
    const stmt = statements[i];
    const safeName = `${stmt.account}_${stmt.subaccount}_${stmt.period}`.replace(/[^a-zA-Z0-9_-]/g, '_');

    console.log(`[CDP] ${i+1}/${statements.length}: ${stmt.account} - ${stmt.subaccount} (${stmt.period})`);

    // Click the button
    await Runtime.evaluate({
      expression: `
        const buttons = Array.from(document.querySelectorAll('button[value]')).filter(b => b.value.includes(',') && b.querySelector('svg'));
        buttons[${i}]?.click();
      `
    });

    // Wait for download
    await new Promise(r => setTimeout(r, 1500));
    downloadedCount++;
    downloaded.push(stmt);
  }

  console.log(`\n[CDP] Downloaded ${downloadedCount} statements to ${DOWNLOAD_DIR}`);

  // Save manifest
  fs.writeFileSync(path.join(DOWNLOAD_DIR, 'manifest.json'), JSON.stringify({
    downloadedAt: new Date().toISOString(),
    count: downloadedCount,
    statements: downloaded
  }, null, 2));

  await client.close();
}

main().catch(e => { console.error(e); process.exit(1); });
