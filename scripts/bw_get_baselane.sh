#!/usr/bin/env bash
set -euo pipefail

ITEM_NAME="Baselane"
ROOT="${OPENCLAW_WORKSPACE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

if ! command -v bw >/dev/null 2>&1; then
  echo "bw CLI not found. Install with: npm install -g @bitwarden/cli" >&2
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "jq not found. Install with: sudo apt-get install -y jq" >&2
  exit 1
fi

export BW_NOINTERACTION="${BW_NOINTERACTION:-true}"
BW_ENV="${BW_ENV:-$ROOT/.secrets/bw.env}"
if [[ -z "${BW_SESSION:-}" && -f /home/digit/.openclaw/.env ]]; then
  # shellcheck disable=SC1091
  set -a; source /home/digit/.openclaw/.env 2>/dev/null || true; set +a
fi

if [[ -z "${BW_SESSION:-}" ]]; then
  ENSURE_SCRIPT="$ROOT/scripts/bw_ensure_session.sh"
  if [[ -x "$ENSURE_SCRIPT" ]]; then
    BW_ENV="$BW_ENV" "$ENSURE_SCRIPT" >/dev/null
  fi

  # shellcheck disable=SC1090
  set -a; source "$BW_ENV" 2>/dev/null || true; set +a
fi

if [[ -z "${BW_SESSION:-}" ]]; then
  echo "BW_SESSION unavailable after ensure step" >&2
  exit 1
fi

FIELD="${1:-json}"
case "$FIELD" in
  username)
    bw get item "$ITEM_NAME" --session "$BW_SESSION" | jq -r '.login.username'
    ;;
  password)
    bw get item "$ITEM_NAME" --session "$BW_SESSION" | jq -r '.login.password'
    ;;
  json)
    bw get item "$ITEM_NAME" --session "$BW_SESSION"
    ;;
  *)
    echo "Usage: $0 [username|password|json]" >&2
    exit 2
    ;;
esac
