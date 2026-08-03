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
PREPARE_ONLY="${CITADEL_CAPTURE_PREPARE_ONLY:-${CITADEL_PREPARE_ONLY:-0}}"
DIAGNOSTICS_REPORT="${CITADEL_HAR_DIAGNOSTICS_REPORT:-$ROOT/reports/citadel_har_workflow_diagnostics_report.json}"

if [ -z "${CITADEL_REQUIRED_RESPONSE_PATHS_JSON:-}" ] && [ -f "$DIAGNOSTICS_REPORT" ]; then
  CITADEL_REQUIRED_RESPONSE_PATHS_JSON="$("$PY" - "$DIAGNOSTICS_REPORT" <<'PY'
import json
import sys
from pathlib import Path

p = Path(sys.argv[1])
try:
    data = json.loads(p.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(0)
next_action = data.get("next_action") if isinstance(data.get("next_action"), dict) else {}
paths = next_action.get("required_response_paths") or data.get("missing_response_body_paths") or []
if isinstance(paths, list):
    cleaned = [item for item in paths if isinstance(item, str) and item.strip()]
    if cleaned:
        print(json.dumps(cleaned))
PY
)"
  export CITADEL_REQUIRED_RESPONSE_PATHS_JSON
fi

if [ -z "${CITADEL_REQUIRED_RESPONSE_PATH_COUNTS_JSON:-}" ] && [ -f "$DIAGNOSTICS_REPORT" ]; then
  CITADEL_REQUIRED_RESPONSE_PATH_COUNTS_JSON="$("$PY" - "$DIAGNOSTICS_REPORT" <<'PY'
import json
import sys
from pathlib import Path

p = Path(sys.argv[1])
try:
    data = json.loads(p.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(0)
next_action = data.get("next_action") if isinstance(data.get("next_action"), dict) else {}
counts = next_action.get("required_response_path_counts") or data.get("missing_response_body_path_counts") or {}
if isinstance(counts, dict):
    cleaned = {}
    for key, value in counts.items():
        if not isinstance(key, str) or not key.strip():
            continue
        try:
            count = int(value)
        except Exception:
            continue
        if count > 0:
            cleaned[key] = count
    if cleaned:
        print(json.dumps(cleaned, sort_keys=True))
PY
)"
  export CITADEL_REQUIRED_RESPONSE_PATH_COUNTS_JSON
fi

if [ -z "${CITADEL_RESPONSE_BODY_REQUIREMENTS_JSON:-}" ] && [ -f "$DIAGNOSTICS_REPORT" ]; then
  CITADEL_RESPONSE_BODY_REQUIREMENTS_JSON="$("$PY" - "$DIAGNOSTICS_REPORT" <<'PY'
import json
import sys
from pathlib import Path

p = Path(sys.argv[1])
try:
    data = json.loads(p.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(0)
next_action = data.get("next_action") if isinstance(data.get("next_action"), dict) else {}
requirements = next_action.get("response_body_requirements") or data.get("response_body_requirements") or []
def as_int(value, fallback):
    try:
        parsed = int(value)
    except Exception:
        return fallback
    return parsed if parsed >= 0 else fallback
if isinstance(requirements, list):
    cleaned = []
    for item in requirements:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if not isinstance(path, str) or not path.strip():
            continue
        cleaned.append({
            "path": path.strip(),
            "role": str(item.get("role") or "workflow_response"),
            "missing_response_body_count": as_int(item.get("missing_response_body_count"), 1),
            "direct_pdf_missing_response_count": as_int(item.get("direct_pdf_missing_response_count"), 0),
            "required_capture_quality": str(item.get("required_capture_quality") or "full_response_body"),
        })
    if cleaned:
        print(json.dumps(cleaned, sort_keys=True))
PY
)"
  export CITADEL_RESPONSE_BODY_REQUIREMENTS_JSON
fi

echo "[citadel] Opening/using a CDP-visible Citadel tab. Complete login and OTP in that browser window."
echo "[citadel] Portal: $PORTAL_URL"
echo "[citadel] Capturing sanitized workflow HAR after authentication."

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
  echo "[citadel] CITADEL_CAPTURE_PREPARE_ONLY=1; prepared visible Citadel tab and pruned stale login tabs only."
  exit 0
fi

WORKSPACE_ROOT="$ROOT" \
CITADEL_AUTH_WAIT_MS="$AUTH_WAIT_MS" \
CITADEL_TAB_SCAN_TARGET_ID="$TARGET_ID" \
CITADEL_TAB_SCAN_LIMIT="${CITADEL_TAB_SCAN_LIMIT:-3}" \
CITADEL_TAB_SCAN_FETCH_TIMEOUT_MS="${CITADEL_TAB_SCAN_FETCH_TIMEOUT_MS:-3000}" \
CITADEL_REQUIRED_RESPONSE_PATHS_JSON="${CITADEL_REQUIRED_RESPONSE_PATHS_JSON:-}" \
CITADEL_REQUIRED_RESPONSE_PATH_COUNTS_JSON="${CITADEL_REQUIRED_RESPONSE_PATH_COUNTS_JSON:-}" \
CITADEL_RESPONSE_BODY_REQUIREMENTS_JSON="${CITADEL_RESPONSE_BODY_REQUIREMENTS_JSON:-}" \
node "$ROOT/scripts/citadel_capture_replay_har.js"
