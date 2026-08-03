#!/usr/bin/env bash
# Self-healing Baselane session seeder (called as a pre-flight by baselane_cron_run.sh).
# No-op if the CDP browser already has an authenticated Baselane session; otherwise
# logs in using the Baselane Bitwarden item so the daily export has valid cookies.
set -euo pipefail
export PATH="${HOME}/.local/bin:${HOME}/.npm-global/bin:/home/digit/.npm-global/bin:/home/linuxbrew/.linuxbrew/bin:$PATH"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# This repository may live at workspace/repos/baselane-lofty-cpai. Resolve the
# canonical OpenClaw home directly instead of assuming the repo is the
# workspace root.
OPENCLAW_ROOT="${OPENCLAW_HOME:-${HOME}/.openclaw}"
export BASELANE_CDP_VERSION_URL="${BASELANE_CDP_VERSION_URL:-http://127.0.0.1:19222/json/version}"
BW_ENV="${BW_ENV:-$ROOT/.secrets/bw.env}"
BW_COMMAND_TIMEOUT_SECONDS="${BW_COMMAND_TIMEOUT_SECONDS:-20}"
log() { echo "[baselane-seed] $*" >&2; }
run_bounded() {
  if command -v timeout >/dev/null 2>&1; then
    timeout "$BW_COMMAND_TIMEOUT_SECONDS" "$@"
  else
    "$@"
  fi
}
# Ensure a valid bw session (patched bw_ensure_session.sh prefers ~/.openclaw/.env BW_SESSION)
log "ensuring Bitwarden session"
run_bounded "$SCRIPT_DIR/bw_ensure_session.sh" >/dev/null 2>&1 || log "WARN: Bitwarden session helper did not complete cleanly"
set -a; [ -f "$BW_ENV" ] && source "$BW_ENV" 2>/dev/null || true; set +a
set -a; [ -f "$OPENCLAW_ROOT/.env" ] && source "$OPENCLAW_ROOT/.env" 2>/dev/null || true; set +a
ITEM_ID="${BASELANE_BW_ITEM_ID:-48221766-44af-4790-a6b5-b3fc00707d55}"
export BASELANE_ITEM_ID="$ITEM_ID"
log "retrieving Baselane credential item"
CREDS="$(BW_NOINTERACTION=true run_bounded bw get item "$ITEM_ID" --session "${BW_SESSION:-}" 2>/dev/null || true)"
if [ -z "$CREDS" ] && [ -n "${BW_MASTER_KEY:-}" ]; then
  log "initial credential read failed; attempting Bitwarden unlock"
  export BW_CLIENTID="${BW_CLIENT_ID:-${BW_CLIENTID:-}}"
  export BW_CLIENTSECRET="${BW_CLIENT_SECRET:-${BW_CLIENTSECRET:-}}"
  if [ -n "${BW_CLIENTID:-}" ] && [ -n "${BW_CLIENTSECRET:-}" ]; then
    BW_NOINTERACTION=true run_bounded bw login --apikey >/dev/null 2>&1 || true
  fi
  NEW_BW_SESSION="$(BW_NOINTERACTION=true run_bounded bw unlock --passwordenv BW_MASTER_KEY --raw 2>/dev/null || true)"
  if [ -n "$NEW_BW_SESSION" ]; then
    BW_SESSION="$NEW_BW_SESSION"
    export BW_SESSION
    CREDS="$(BW_NOINTERACTION=true run_bounded bw get item "$ITEM_ID" --session "$BW_SESSION" 2>/dev/null || true)"
  fi
fi
export BASELANE_EMAIL="$(printf '%s' "$CREDS" | python3 -c 'import sys,json;d=json.load(sys.stdin);print((d.get("login") or {}).get("username",""))' 2>/dev/null || true)"
export BASELANE_PASSWORD="$(printf '%s' "$CREDS" | python3 -c 'import sys,json;d=json.load(sys.stdin);print((d.get("login") or {}).get("password",""))' 2>/dev/null || true)"
if [ -z "${BASELANE_EMAIL:-}" ] || [ -z "${BASELANE_PASSWORD:-}" ]; then
  echo "[baselane-seed] ERROR: could not retrieve Baselane creds from Bitwarden" >&2
  exit 1
fi
export BASELANE_SESSION_SEED_TIMEOUT_SECONDS="${BASELANE_SESSION_SEED_TIMEOUT_SECONDS:-180}"
AUTH_CHECK=(python3 "$SCRIPT_DIR/baselane_cdp_auth_recovery.py" --graphql-auth-smoke)
run_python_seed() {
  log "starting Python CDP session seed via ${BASELANE_CDP_VERSION_URL:-default CDP endpoint}"
  if command -v timeout >/dev/null 2>&1; then
    timeout "$BASELANE_SESSION_SEED_TIMEOUT_SECONDS" python3 "$SCRIPT_DIR/baselane_seed_session_via_cdp.py"
  else
    python3 "$SCRIPT_DIR/baselane_seed_session_via_cdp.py"
  fi
}
run_canonical_login_wait() {
  log "running canonical visible CDP login-and-wait fallback"
  BASELANE_FORCE_LOGIN=1 \
  BASELANE_LOGIN_WAIT_MS="${BASELANE_LOGIN_WAIT_MS:-1000}" \
  BASELANE_LOGIN_WAIT_TIMEOUT_MS="${BASELANE_LOGIN_WAIT_TIMEOUT_MS:-90000}" \
  BASELANE_AUTHENTICATED_CONTENT_TIMEOUT_MS="${BASELANE_AUTHENTICATED_CONTENT_TIMEOUT_MS:-45000}" \
    node "$SCRIPT_DIR/baselane_login_and_wait_via_cdp.js"
}
print_auth_hold_state() {
  python3 - "$ROOT/reports/baselane_login_wait_report.json" <<'PY'
import json
import pathlib
import sys

p = pathlib.Path(sys.argv[1])
d=json.loads(p.read_text()) if p.is_file() else {}
keys=("status","reason","challenge_type","current_url","next_action","resume_command")
print(json.dumps({k:d.get(k) for k in keys if d.get(k) is not None}, indent=2, sort_keys=True))
PY
}

# Fast path: the lightweight seeder usually establishes the session. Its
# success is provisional until the independent GraphQL auth gate passes.
if run_python_seed && "${AUTH_CHECK[@]}"; then
  log "authenticated Baselane session verified"
  exit 0
fi

# Deterministic fallback: use the maintained real keyboard/mouse CDP flow,
# then require the same independent GraphQL proof. Route changes alone are not
# accepted as authentication because Baselane may briefly render its app shell
# before redirecting back to /login.
if ! run_canonical_login_wait; then
  log "Baselane login pipeline is holding at a reported authentication state"
  print_auth_hold_state
  exit 5
fi
if "${AUTH_CHECK[@]}"; then
  log "authenticated Baselane session verified after canonical fallback"
  exit 0
fi

log "ERROR: Baselane authentication was not verified after maintained login workflows"
exit 5
