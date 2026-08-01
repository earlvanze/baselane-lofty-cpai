#!/usr/bin/env python3
"""
Apply Wyoming SOS splits to Baselane via CDP/GraphQL
Uses credentials from Bitwarden to authenticate first
"""

import json
import subprocess
import os
import sys
from pathlib import Path

ROOT = Path('/home/digit/.openclaw/workspace')
CDP_URL = 'http://127.0.0.1:9222/json/version'
GRAPHQL_URL = 'https://orchestration.baselane.com/graphql'

def get_bw_creds():
    """Get Baselane credentials from Bitwarden"""
    env_file = Path('/home/digit/.openclaw/.env')
    bw_master = None

    if env_file.exists():
        for line in env_file.read_text().split('\n'):
            if line.startswith('BW_MASTER_KEY='):
                bw_master = line.split('=', 1)[1]
                break

    if not bw_master:
        raise RuntimeError('BW_MASTER_KEY not found')

    # Unlock Bitwarden
    result = subprocess.run(
        ['bw', 'unlock', '--passwordenv', 'BW_MASTER_KEY', '--raw'],
        capture_output=True, text=True,
        env={**os.environ, 'BW_MASTER_KEY': bw_master}
    )

    if result.returncode != 0:
        raise RuntimeError(f'Bitwarden unlock failed: {result.stderr}')

    bw_session = result.stdout.strip()

    # Get item
    result = subprocess.run(
        ['bw', 'get', 'item', '48221766-44af-4790-a6b5-b3fc00707d55', '--session', bw_session],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        raise RuntimeError(f'Bitwarden get item failed: {result.stderr}')

    item = json.loads(result.stdout)
    login = item.get('login', {})

    return {
        'email': login.get('username'),
        'password': login.get('password')
    }


def main():
    # Load split plan
    plan_path = ROOT / 'reports' / 'baselane_wyoming_split_plan.json'
    with open(plan_path) as f:
        plan = json.load(f)

    splits = plan.get('split_plan', [])
    print(f"Loaded {len(splits)} Wyoming splits to apply")

    # Get credentials
    creds = get_bw_creds()
    print(f"Credentials obtained for: {creds['email']}")

    # Save credentials to temp file for JS script
    creds_file = ROOT / 'tmp' / 'baselane_creds.json'
    creds_file.parent.mkdir(exist_ok=True)
    with open(creds_file, 'w') as f:
        json.dump(creds, f)

    # Create and run login + apply script
    js_code = '''
const fs = require('fs');
const CDP_URL = 'http://127.0.0.1:9222/json/version';
const LOGIN_URL = 'https://app.baselane.com/login';
const TARGET_URL = 'https://app.baselane.com/transactions';
const GRAPHQL_URL = 'https://orchestration.baselane.com/graphql';

const creds = JSON.parse(fs.readFileSync('/home/digit/.openclaw/workspace/tmp/baselane_creds.json', 'utf8'));

async function main() {
    console.error('[1] Connecting to CDP...');
    const version = await (await fetch(CDP_URL)).json();
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
            const t = setTimeout(() => { pending.delete(msg.id); rej(new Error('timeout')); }, 30000);
            pending.set(msg.id, {
                resolve: v => { clearTimeout(t); res(v); },
                reject: e => { clearTimeout(t); rej(e); }
            });
        });
    }

    // Create new tab for login
    console.error('[2] Creating Baselane tab...');
    const created = await send('Target.createTarget', { url: LOGIN_URL });
    await new Promise(r => setTimeout(r, 3000));

    const attached = await send('Target.attachToTarget', { targetId: created.targetId, flatten: true });
    const sessionId = attached.sessionId;

    // Fill login form
    console.error('[3] Filling login form...');

    await send('Runtime.evaluate', {
        expression: `document.querySelector('input[type="email"]').value = '${creds.email}';`,
        returnByValue: true
    }, sessionId);

    await send('Runtime.evaluate', {
        expression: `document.querySelector('input[type="password"]').value = '${creds.password}';`,
        returnByValue: true
    }, sessionId);

    // Click submit
    await send('Runtime.evaluate', {
        expression: `document.querySelector('button[type="submit"]').click();`,
        returnByValue: true
    }, sessionId);

    console.error('[4] Waiting for authentication...');
    await new Promise(r => setTimeout(r, 5000));

    // Check if logged in
    const check = await send('Runtime.evaluate', {
        expression: 'window.location.href',
        returnByValue: true
    }, sessionId);

    const url = check.result?.value || '';
    console.error(`[5] Current URL: ${url}`);

    if (!url.includes('/transactions') && !url.includes('/banking')) {
        console.error('Login failed or 2FA required');
        process.exit(1);
    }

    console.error('[6] Successfully authenticated!');
    console.log(JSON.stringify({ success: true, url: url }));

    ws.close();
}

main().catch(err => {
    console.error('Error:', err.message);
    process.exit(1);
});
'''

    js_file = ROOT / 'tmp' / 'baselane_login_apply.js'
    with open(js_file, 'w') as f:
        f.write(js_code)

    print("\n[7] Running CDP login script...")
    result = subprocess.run(
        ['node', str(js_file)],
        capture_output=True, text=True, timeout=60
    )

    print(result.stderr)

    if result.returncode == 0:
        print("\n✓ Authentication successful!")
        print("Ready to apply Wyoming splits...")
        # Here we would apply the splits via GraphQL
        print(f"Would apply {len(splits)} splits")
    else:
        print(f"\n✗ Login failed: {result.stderr}")
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
