#!/usr/bin/env bash
set -euo pipefail

export PATH="${HOME}/.local/bin:/home/umbrel/.local/bin:${HOME}/.npm-global/bin:/home/digit/.npm-global/bin:$PATH"


SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# This repository may live at workspace/repos/baselane-lofty-cpai. Resolve the
# canonical OpenClaw home directly instead of assuming the repo is the
# workspace root.
OPENCLAW_ROOT="${OPENCLAW_HOME:-${HOME}/.openclaw}"
BW_ENV_DEFAULT="$ROOT/.secrets/bw.env"
BW_ENV="${BW_ENV:-$BW_ENV_DEFAULT}"
ENV_FALLBACK="${OPENCLAW_ROOT}/.env"
if ! command -v bw >/dev/null 2>&1; then
  echo "ERROR: bitwarden CLI 'bw' not found in PATH" >&2
  exit 1
fi

mkdir -p "$(dirname "$BW_ENV")"
[ -f "$BW_ENV" ] || touch "$BW_ENV"

update_env_session() {
  local env_file="$1"
  local session_value="$2"
  local tmp
  [ -n "$env_file" ] || return 0
  [ -f "$env_file" ] || return 0
  tmp="$(mktemp)"
  if grep -q '^BW_SESSION=' "$env_file"; then
    awk -v s="$session_value" 'BEGIN{done=0} /^BW_SESSION=/{print "BW_SESSION=" s; done=1; next} {print} END{if(!done) print "BW_SESSION=" s}' "$env_file" > "$tmp"
  else
    cat "$env_file" > "$tmp"
    printf '\nBW_SESSION=%s\n' "$session_value" >> "$tmp"
  fi
  mv "$tmp" "$env_file"
  chmod 600 "$env_file" || true
}

session_is_valid() {
  local session_value="${1:-}"
  [ -n "$session_value" ] || return 1
  bw unlock --check --session "$session_value" >/dev/null 2>&1
}

# Load helper-local metadata first, then the canonical OpenClaw env so cron has
# BW_MASTER_KEY/API keys even when .secrets/bw.env already exists.
# shellcheck disable=SC1090
set -a; source "$BW_ENV" 2>/dev/null || true; set +a
if [ -f "$ENV_FALLBACK" ]; then
  # shellcheck disable=SC1090
  set -a; source "$ENV_FALLBACK" 2>/dev/null || true; set +a
fi

# Prefer the canonical live BW_SESSION from ~/.openclaw/.env if valid; bw.env
# may hold a stale fallback session from an older run.
if [ -f "$ENV_FALLBACK" ]; then
  _env_session="$(set -a; source "$ENV_FALLBACK" 2>/dev/null; printf '%s' "${BW_SESSION:-}")"
  if session_is_valid "$_env_session"; then
    BW_SESSION="$_env_session"
  fi
  unset _env_session
fi
export BW_SESSION

status_json="$(bw status 2>/dev/null || echo '{"status":"unauthenticated"}')"
status="$(printf '%s' "$status_json" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("status","unauthenticated"))' 2>/dev/null || echo unauthenticated)"

session_valid=false
if session_is_valid "${BW_SESSION:-}"; then
  session_valid=true
fi

if $session_valid; then
  echo "OK: BW_SESSION already valid"
  exit 0
fi

if [ -n "${BW_MASTER_KEY:-}" ]; then
  export BW_CLIENTID="${BW_CLIENT_ID:-${BW_CLIENTID:-}}"
  export BW_CLIENTSECRET="${BW_CLIENT_SECRET:-${BW_CLIENTSECRET:-}}"
  if [ -n "${BW_CLIENTID:-}" ] && [ -n "${BW_CLIENTSECRET:-}" ]; then
    BW_NOINTERACTION=true bw login --apikey >/dev/null 2>&1 || true
  fi
  new_session="$(BW_NOINTERACTION=true bw unlock --passwordenv BW_MASTER_KEY --raw)"
  if [ -z "$new_session" ]; then
    echo "ERROR: Failed to retrieve BW_SESSION using BW_MASTER_KEY" >&2
    exit 3
  fi
  update_env_session "$BW_ENV" "$new_session"
  if [ "${BW_UPDATE_OPENCLAW_ENV:-1}" = "1" ]; then
    update_env_session "$ENV_FALLBACK" "$new_session"
  fi
  echo "OK: BW_SESSION refreshed non-interactively and saved to $BW_ENV"
  exit 0
fi

if [ "$status" = "unauthenticated" ]; then
  echo "ERROR: Not logged in to Bitwarden CLI. Run 'bw login' first." >&2
  exit 2
fi

# Requires interactive unlock if currently locked or session invalid.
new_session="$(bw unlock --raw)"
if [ -z "$new_session" ]; then
  echo "ERROR: Failed to retrieve BW_SESSION" >&2
  exit 3
fi

update_env_session "$BW_ENV" "$new_session"
if [ "${BW_UPDATE_OPENCLAW_ENV:-1}" = "1" ]; then
  update_env_session "$ENV_FALLBACK" "$new_session"
fi

echo "OK: BW_SESSION refreshed and saved to $BW_ENV"
