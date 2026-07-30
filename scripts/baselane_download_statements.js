#!/usr/bin/env node
const CDP = require('chrome-remote-interface');
const fs = require('fs');
const path = require('path');
const https = require('https');

const DOWNLOAD_DIR = process.env.DOWNLOAD_DIR || path.join(process.env.HOME, '.openclaw/workspace/baselane-statements');
const YEAR_FILTER = process.env.YEAR_FILTER || '2026';
const MONTH_FILTER = process.env.MONTH_FILTER || 'All';

async function downloadFile(url, filepath, cookies) {
  return new Promise((resolve, reject) => {
    const file = fs.createWriteStream(filepath);
    const options = new URL(url);
    options.headers = { Cookie: cookies };

    https.get(options, response => {
      if (response.statusCode === 302 || response.statusCode === 301) {
        // Follow redirect
        downloadFile(response.headers.location, filepath, cookies).then(resolve).catch(reject);
        return;
      }
      response.pipe(file);
      file.on('finish', () => { file.close(); resolve(filepath); });
    }).on('error', err => {
      fs.unlink(filepath, () => {});
      reject(err);
    });
  });
}

async function main() {
  fs.mkdirSync(DOWNLOAD_DIR, {recursive: true});

  const targets = await CDP.List();
  const baselaneTarget = targets.find(t => t.url.includes('baselane.com'));

  if (!baselaneTarget) {
    console.error('No Baselane tab found');
    process.exit(1);
  }

  const client = await CDP({target: baselaneTarget.id});
  const {Page, Runtime, Network} = client;

  await Page.enable();
  await Network.enable();

  // Track download URLs
  const downloadUrls = [];
  Network.responseReceived(params => {
    if (params.response.url.includes('statement') || params.response.url.includes('pdf')) {
      downloadUrls.push(params.response.url);
    }
  });

  // Navigate to statements
  console.log('[CDP] Navigating to statements page...');
  await Page.navigate({url: 'https://app.baselane.com/banking/statements'});
  await new Promise(r => setTimeout(r, 5000));

  // Get all download buttons
  const buttons = await Runtime.evaluate({
    expression: `
      Array.from(document.querySelectorAll('button.chakra-button')).filter(b =>
        b.textContent?.trim() === 'Download'
      ).length
    `,
    returnByValue: true
  });

  const numStatements = buttons.result.value;
  console.log('[CDP] Found', numStatements, 'statements to download');

  // Click each download button and track
  for (let i = 0; i < Math.min(numStatements, 100); i++) {
    console.log(`[CDP] Processing statement ${i + 1}/${numStatements}...`);

    // Get statement info before clicking
    const info = await Runtime.evaluate({
      expression: `
        const buttons = Array.from(document.querySelectorAll('button.chakra-button')).filter(b => b.textContent?.trim() === 'Download');
        const btn = buttons[${i}];
        if (btn) {
          const row = btn.closest('tr') || btn.parentElement?.parentElement;
          const text = row?.innerText || '';
          ({text, index: ${i}});
        }
      `,
      returnByValue: true
    });

    console.log(`[CDP] Statement: ${info.result.value?.text?.replace(/\n/g, ' | ')}`);

    // Click the download button
    await Runtime.evaluate({
      expression: `
        const buttons = Array.from(document.querySelectorAll('button.chakra-button')).filter(b => b.textContent?.trim() === 'Download');
        buttons[${i}]?.click();
      `
    });

    // Wait for download to start
    await new Promise(r => setTimeout(r, 1500));
  }

  console.log('[CDP] Download URLs captured:', downloadUrls.length);
  console.log('[CDP] Done!');

  await client.close();
}

main().catch(e => { console.error(e); process.exit(1); });
