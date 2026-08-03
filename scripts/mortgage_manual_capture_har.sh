#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${WORKSPACE_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
PY="${PYTHON_BIN:-python3}"
CDP_JSON_BASE="${MORTGAGE_WORKFLOW_CDP_JSON_BASE:-${BASELANE_CDP_JSON_BASE:-http://127.0.0.1:9222}}"
PROPERTY="${MORTGAGE_WORKFLOW_PROPERTY:?MORTGAGE_WORKFLOW_PROPERTY is required}"
PORTAL_URL="${MORTGAGE_WORKFLOW_PORTAL_URL:?MORTGAGE_WORKFLOW_PORTAL_URL is required}"
HAR_PATH="${MORTGAGE_WORKFLOW_HAR_PATH:?MORTGAGE_WORKFLOW_HAR_PATH is required}"
NEW_TAB="${MORTGAGE_WORKFLOW_NEW_TAB:-0}"
AUTH_WAIT_MS="${MORTGAGE_WORKFLOW_AUTH_WAIT_MS:-300000}"
PREPARE_ONLY="${MORTGAGE_WORKFLOW_CAPTURE_PREPARE_ONLY:-0}"

mkdir -p "$(dirname "$HAR_PATH")"

echo "[mortgage] Property: $PROPERTY"
echo "[mortgage] Portal: $PORTAL_URL"
echo "[mortgage] HAR target: $HAR_PATH"
echo "[mortgage] Use the visible browser. Do not run this with a headless browser."

TARGET_ID="${MORTGAGE_WORKFLOW_TAB_SCAN_TARGET_ID:-}"
if [ -z "$TARGET_ID" ]; then
  TARGET_ID="$("$PY" - "$CDP_JSON_BASE" "$PORTAL_URL" "$NEW_TAB" <<'PY'
import json
import sys
import urllib.parse
import urllib.request
from urllib.parse import urlparse

base = sys.argv[1].rstrip("/")
portal_url = sys.argv[2]
new_tab = sys.argv[3] == "1"
portal_host = urlparse(portal_url if "://" in portal_url else f"https://{portal_url}").netloc.lower()

def request_json(path, method="GET"):
    req = urllib.request.Request(base + path, method=method)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.load(resp)

def score(tab):
    url = str(tab.get("url") or "").lower()
    title = str(tab.get("title") or "").lower()
    value = 0
    if portal_host and portal_host in url:
        value += 100
    if portal_host and portal_host.split(".")[-2:] and ".".join(portal_host.split(".")[-2:]) in url:
        value += 25
    if portal_host and portal_host.split(".")[0] in title:
        value += 10
    return value

target_id = ""
tabs = []
if not new_tab:
    try:
        tabs = request_json("/json/list")
        candidates = [tab for tab in tabs if score(tab) > 0 and tab.get("id")]
        candidates.sort(key=score, reverse=True)
        if candidates:
            target_id = str(candidates[0].get("id") or "")
    except Exception:
        target_id = ""

if not target_id:
    try:
        target = request_json("/json/new?" + urllib.parse.quote(portal_url, safe=""), method="PUT")
        target_id = str(target.get("id") or "")
    except Exception:
        target_id = ""

if target_id:
    try:
        urllib.request.urlopen(base + "/json/activate/" + urllib.parse.quote(target_id, safe=""), timeout=5).read()
    except Exception:
        pass
print(target_id)
PY
)"
fi

if [ -z "$TARGET_ID" ]; then
  echo "[mortgage] Could not create or locate a CDP browser tab at $CDP_JSON_BASE." >&2
  echo "[mortgage] Start Brave/Chrome with remote debugging on port 9222, then rerun the same command." >&2
  exit 1
fi

echo "[mortgage] CDP target: $TARGET_ID"
if [ "$PREPARE_ONLY" = "1" ]; then
  echo "[mortgage] MORTGAGE_WORKFLOW_CAPTURE_PREPARE_ONLY=1; opened/activated tab only."
  exit 0
fi

WORKSPACE_ROOT="$ROOT" \
MORTGAGE_WORKFLOW_PROPERTY="$PROPERTY" \
MORTGAGE_WORKFLOW_PORTAL_URL="$PORTAL_URL" \
MORTGAGE_WORKFLOW_HAR_PATH="$HAR_PATH" \
MORTGAGE_WORKFLOW_AUTH_WAIT_MS="$AUTH_WAIT_MS" \
MORTGAGE_WORKFLOW_TAB_SCAN_TARGET_ID="$TARGET_ID" \
MORTGAGE_WORKFLOW_CDP_JSON_BASE="$CDP_JSON_BASE" \
node "$ROOT/scripts/mortgage_capture_visible_har.js"
