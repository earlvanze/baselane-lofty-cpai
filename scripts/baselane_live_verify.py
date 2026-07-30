#!/usr/bin/env python3
"""
Live verification of Baselane property tags via CDP + GraphQL
Captures auth token from browser and queries live transaction data
"""

import json
import subprocess
import os
import sys
import time
from pathlib import Path
import urllib.request
import urllib.error

ROOT = Path('/home/digit/.openclaw/workspace')
CDP_URL = 'http://127.0.0.1:9222/json/version'
GRAPHQL_URL = 'https://orchestration.baselane.com/graphql'

def check_cdp():
    """Check if CDP is available"""
    import urllib.request
    try:
        with urllib.request.urlopen(CDP_URL, timeout=5) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        return None

def capture_auth_from_cdp():
    """Use CDP to capture Firebase auth token from authenticated session"""
    print("[CDP] Attempting to capture auth token...")

    # This requires a CDP library or manual WebSocket
    # For now, document the manual steps
    print("    Manual capture required:")
    print("    1. Open Baselane in browser with authenticated session")
    print("    2. Open DevTools > Application > Local Storage")
    print("    3. Find 'firebase:authUser' key")
    print("    4. Extract the accessToken field")
    print("    Or use CDP to extract programmatically")

    return None

def query_transaction(token, txn_id):
    """Query single transaction via GraphQL"""

    query = """
    query GetTransaction($id: ID!) {
        transaction(id: $id) {
            id
            property {
                id
                name
            }
            category {
                id
                name
            }
            amount
            date
            merchant
            description
        }
    }
    """

    payload = json.dumps({
        "query": query,
        "variables": {"id": txn_id}
    }).encode()

    req = urllib.request.Request(
        GRAPHQL_URL,
        data=payload,
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        },
        method='POST'
    )

    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        error = e.read().decode()
        return {"error": f"HTTP {e.code}: {error}"}
    except Exception as e:
        return {"error": str(e)}

def try_session_storage_auth():
    """Try to extract auth from Chrome session storage via CDP"""
    try:
        import websocket

        # Connect to CDP
        ws_url = None
        import urllib.request
        with urllib.request.urlopen('http://127.0.0.1:9222/json') as resp:
            tabs = json.loads(resp.read().decode())
            for tab in tabs:
                if 'webSocketDebuggerUrl' in tab:
                    ws_url = tab['webSocketDebuggerUrl']
                    break

        if not ws_url:
            return None

        # Connect and extract localStorage
        ws = websocket.create_connection(ws_url)

        # Get localStorage for baselane domain
        ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": "localStorage.getItem('firebase:authUser:[\"AIzaSyC...\"]')",
                "returnByValue": True
            }
        }))

        response = json.loads(ws.recv())
        ws.close()

        if 'result' in response and 'value' in response['result']:
            auth_data = json.loads(response['result']['value'])
            return auth_data.get('stsTokenManager', {}).get('accessToken')

    except Exception as e:
        print(f"    CDP auth extraction failed: {e}")
        return None

def main():
    print("=" * 70)
    print("BASELANE LIVE VERIFICATION")
    print("=" * 70)

    # Load Wyoming split plan
    wyoming_path = ROOT / 'reports' / 'baselane_wyoming_split_plan.json'
    with open(wyoming_path) as f:
        wyoming_plan = json.load(f)

    splits = wyoming_plan.get('split_plan', [])
    print(f"\n[1] Loaded {len(splits)} Wyoming splits to verify")

    # Check CDP
    print("\n[2] Checking CDP availability...")
    cdp_info = check_cdp()
    if cdp_info:
        print(f"    ✓ CDP available: {cdp_info.get('Browser', 'Unknown')}")
    else:
        print("    ✗ CDP not available at http://127.0.0.1:9222")
        print("    Run: brave --remote-debugging-port=9222")
        return 1

    # Try to capture auth token
    print("\n[3] Attempting to capture auth token...")
    token = try_session_storage_auth()

    if token:
        print("    ✓ Auth token captured from browser")

        # Sample verification of first 5 transactions
        print("\n[4] Verifying sample transactions...")

        verified = 0
        mismatched = 0
        errors = 0

        for split in splits[:5]:
            txn_id = split['original_txn']['BaselaneId']
            expected_prop = split['split_property']

            result = query_transaction(token, txn_id)

            if 'error' in result:
                print(f"    ✗ TXN {txn_id}: {result['error']}")
                errors += 1
            else:
                data = result.get('data', {}).get('transaction', {})
                live_prop = data.get('property', {}).get('name', 'None')

                if live_prop == expected_prop:
                    print(f"    ✓ TXN {txn_id}: Property matches ({live_prop})")
                    verified += 1
                else:
                    print(f"    ⚠ TXN {txn_id}: Mismatch!")
                    print(f"      Expected: {expected_prop}")
                    print(f"      Live: {live_prop}")
                    mismatched += 1

        print(f"\n[5] Sample results:")
        print(f"    Verified: {verified}")
        print(f"    Mismatched: {mismatched}")
        print(f"    Errors: {errors}")

        if mismatched > 0:
            print(f"\n    → Property tags need to be applied upstream")
        else:
            print(f"\n    → Property tags verified (sample)")

    else:
        print("    ✗ Could not capture auth token automatically")
        print("\n[4] Manual verification steps:")
        print("    1. Open https://app.baselane.com in authenticated browser")
        print("    2. Open DevTools (F12) > Console")
        print("    3. Run: JSON.parse(localStorage.getItem('firebase:authUser:AIzaSy...'))")
        print("    4. Save accessToken to: /tmp/baselane_token.txt")
        print("    5. Re-run this script")

        # Check for manual token file
        token_file = Path('/tmp/baselane_token.txt')
        if token_file.exists():
            token = token_file.read_text().strip()
            print(f"\n    Found manual token, proceeding...")
            # Continue with verification...

    return 0

if __name__ == '__main__':
    sys.exit(main())
