#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${WORKSPACE_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
AUTH_WAIT_MS="${CITADEL_AUTH_WAIT_MS:-300000}"
PORTAL_URL="https://loansphereservicingdigital.bkiconnect.com/mycitadelservicing/#/login"
CDP_JSON_BASE="${CITADEL_CDP_JSON_BASE:-http://127.0.0.1:9222}"
PY="${PYTHON_BIN:-python3}"
NEW_TAB="${CITADEL_NEW_TAB:-0}"
PRUNE_LOGIN_TABS="${CITADEL_PRUNE_LOGIN_TABS:-1}"
PREPARE_ONLY="${CITADEL_AUTH_PREPARE_ONLY:-${CITADEL_PREPARE_ONLY:-0}}"

echo "[citadel] Opening/using a CDP-visible Citadel tab. Complete login and OTP in that browser window."
echo "[citadel] Portal: $PORTAL_URL"

TARGET_ID="${CITADEL_TAB_SCAN_TARGET_ID:-}"
if [ -z "$TARGET_ID" ]; then
  TARGET_ID="$("$PY" - "$CDP_JSON_BASE" "$PORTAL_URL" "$NEW_TAB" "$PRUNE_LOGIN_TABS" <<'PY'
import json
import sys
import urllib.parse
import urllib.request

base = sys.argv[1].rstrip("/")
portal_url = sys.argv[2]
new_tab = sys.argv[3] == "1"
prune_login_tabs = sys.argv[4] == "1"

def request_json(path, method="GET"):
    req = urllib.request.Request(base + path, method=method)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.load(resp)

def close_target(target_id):
    quoted = urllib.parse.quote(target_id, safe="")
    for method in ("PUT", "GET"):
        try:
            req = urllib.request.Request(base + "/json/close/" + quoted, method=method)
            urllib.request.urlopen(req, timeout=5).read()
            return True
        except Exception:
            pass
    return False

def score(tab):
    url = tab.get("url") or ""
    title = tab.get("title") or ""
    value = 0
    if "loansphereservicingdigital.bkiconnect.com/mycitadelservicing" in url:
        value += 100
    if "mycitadelservicing" in url or "citadel" in title.lower():
        value += 50
    if "#/login" not in url:
        value += 25
    return value

def is_citadel_login_tab(tab):
    url = tab.get("url") or ""
    return "loansphereservicingdigital.bkiconnect.com/mycitadelservicing/#/login" in url

target_id = ""
list_failed = False
tabs = []
if not new_tab:
    try:
        tabs = request_json("/json/list")
        candidates = [tab for tab in tabs if score(tab) > 0 and tab.get("id")]
        candidates.sort(key=score, reverse=True)
        if candidates:
            target_id = candidates[0].get("id") or ""
    except Exception:
        list_failed = True
        target_id = ""

if not target_id and (new_tab or not list_failed):
    try:
        target = request_json("/json/new?" + urllib.parse.quote(portal_url, safe=""), method="PUT")
        target_id = target.get("id") or ""
    except Exception:
        target_id = ""

if prune_login_tabs and not tabs:
    try:
        tabs = request_json("/json/list")
    except Exception:
        tabs = []

if prune_login_tabs and tabs:
    stale_login_tabs = [tab for tab in tabs if is_citadel_login_tab(tab) and tab.get("id") and tab.get("id") != target_id]
    pruned_count = 0
    for tab in stale_login_tabs:
        if close_target(tab.get("id")):
            pruned_count += 1
    print(f"[citadel] stale login tabs: {len(stale_login_tabs)}; pruned: {pruned_count}", file=sys.stderr)

if target_id:
    try:
        urllib.request.urlopen(base + "/json/activate/" + urllib.parse.quote(target_id, safe=""), timeout=5).read()
    except Exception:
        pass
print(target_id)
PY
)"
fi

if [ -n "$TARGET_ID" ]; then
  echo "[citadel] CDP target: $TARGET_ID"
else
  echo "[citadel] Warning: could not create a pinned CDP target; falling back to Citadel tab scan"
fi

if [ "$PREPARE_ONLY" = "1" ]; then
  echo "[citadel] CITADEL_AUTH_PREPARE_ONLY=1; prepared visible Citadel tab and pruned stale login tabs only."
  exit 0
fi

WORKSPACE_ROOT="$ROOT" \
CITADEL_LOGIN_MODE=manual \
CITADEL_REUSE_TAB=1 \
CITADEL_AUTH_WAIT_MS="$AUTH_WAIT_MS" \
CITADEL_TAB_SCAN_TARGET_ID="$TARGET_ID" \
CITADEL_TAB_SCAN_LIMIT="${CITADEL_TAB_SCAN_LIMIT:-3}" \
CITADEL_TAB_SCAN_FETCH_TIMEOUT_MS="${CITADEL_TAB_SCAN_FETCH_TIMEOUT_MS:-2000}" \
node "$ROOT/scripts/citadel_download_statements.js"
