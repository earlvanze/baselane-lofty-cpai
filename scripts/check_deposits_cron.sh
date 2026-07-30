#!/usr/bin/env bash
# Cron wrapper: check Hemlane + Baselane for tenant rent deposits.
# Schedule: daily at 8 PM Eastern (2 AM CEST), Wed through end of month.
# DMs Earl on Telegram if no deposits found on the first run of the month.
# Created 2026-07-10 per Earl's request in #25-circle-dr.
#
# Integrates Hemlane skill:
# - Uses skills/hemlane/scripts/capture_hemlane_auth_via_cdp.py for fresh auth
# - Uses skills/hemlane/scripts/query_recurring_payment_requests.py patterns
# - Falls back gracefully if CDP/Brave is not available
set -euo pipefail

export PATH="${HOME}/.local/bin:/home/digit/.npm-global/bin:${HOME}/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"
cd /home/digit/.openclaw/workspace

export WORKSPACE_ROOT=/home/digit/.openclaw/workspace
export OPENCLAW_ROOT=/home/digit/.openclaw

LOCKFILE=/tmp/check_deposits_cron.lock
REPORT_DIR="${WORKSPACE_ROOT}/reports"
LOG_DIR="${WORKSPACE_ROOT}/logs"
REPORT_FILE="${REPORT_DIR}/check_deposits_report.json"
STATE_FILE="${REPORT_DIR}/check_deposits_state.json"

mkdir -p "$REPORT_DIR" "$LOG_DIR"

# ── Telegram config ──────────────────────────────────────────────
get_telegram_config() {
  local config_path="${OPENCLAW_ROOT}/openclaw.json"
  python3 -c "
import json, os, sys
config = json.load(open('$config_path')) if os.path.exists('$config_path') else {}
telegram = (config.get('channels') or {}).get('telegram') or {}
env = {}
env_path = os.path.expanduser('${OPENCLAW_ROOT}/.env')
if os.path.exists(env_path):
    for line in open(env_path):
        line = line.strip()
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip().strip('\"').strip(\"'\")
env.update(os.environ)
token = os.environ.get('TELEGRAM_BOT_TOKEN') or os.environ.get('OPENCLAW_BOTTOKEN') or ''
chat_id = os.environ.get('BASELANE_EOD_TELEGRAM_CHAT_ID') or os.environ.get('TELEGRAM_CHAT_ID') or ''
if not token and telegram.get('botToken'):
    ref = telegram['botToken']
    if ref.startswith('\${') and ref.endswith('}'):
        env_key = ref[2:-1]
        token = env.get(env_key, '')
    else:
        token = ref
if not chat_id:
    allow = telegram.get('allowFrom') or telegram.get('groupAllowFrom') or []
    if allow:
        chat_id = str(allow[0])
print(f'{token}\\n{chat_id}')
"
}

send_telegram() {
  local message="$1"
  local config_output token chat_id
  config_output=$(get_telegram_config)
  token=$(echo "$config_output" | head -1)
  chat_id=$(echo "$config_output" | tail -1)

  if [[ -z "$token" || -z "$chat_id" ]]; then
    echo "ERROR: Telegram token or chat_id not configured" >&2
    return 1
  fi

  python3 -c "
import urllib.request, json, sys
token = '''$token'''
chat_id = '''$chat_id'''
message = sys.stdin.read()
url = f'https://api.telegram.org/bot{token}/sendMessage'
payload = json.dumps({
    'chat_id': chat_id,
    'text': message,
    'parse_mode': 'HTML',
    'disable_web_page_preview': True
}).encode()
req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'})
try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode())
        if not result.get('ok'):
            print(f'Telegram API error: {result}', file=sys.stderr)
            sys.exit(1)
        print('Telegram message sent successfully')
except Exception as e:
    print(f'Telegram send failed: {e}', file=sys.stderr)
    sys.exit(1)
" <<< "$message"
}

# ── Main ──────────────────────────────────────────────────────────
main() {
  echo "[$(date -Iseconds)] check_deposits_cron started"

  # Run the deposit checker (integrates Hemlane skill CDP auth capture)
  python3 scripts/check_deposits_hemlane_baselane.py \
    --output "$REPORT_FILE" \
    --state-file "$STATE_FILE" \
    2>&1 || {
    echo "[$(date -Iseconds)] ERROR: deposit checker failed" >&2
    send_telegram "⚠️ Deposit check failed to run. Check logs on Cyber." || true
    exit 1
  }

  if [[ ! -f "$REPORT_FILE" ]]; then
    echo "[$(date -Iseconds)] ERROR: no report file produced" >&2
    exit 1
  fi

  deposits_found=$(python3 -c "import json; r=json.load(open('$REPORT_FILE')); print(r.get('deposits_found', 0))")
  unpaid_count=$(python3 -c "import json; r=json.load(open('$REPORT_FILE')); print(len(r.get('unpaid_properties', [])))")
  is_first_run=$(python3 -c "import json; r=json.load(open('$REPORT_FILE')); print('true' if r.get('is_first_run', False) else 'false')")
  alert_sent=$(python3 -c "import json; r=json.load(open('$REPORT_FILE')); print('true' if r.get('alert_sent', False) else 'false')")

  echo "[$(date -Iseconds)] Deposits found: $deposits_found | Unpaid: $unpaid_count | First run: $is_first_run | Alert sent: $alert_sent"

  if [[ "$alert_sent" == "true" ]]; then
    echo "[$(date -Iseconds)] Telegram DM was sent by the checker script"
  elif [[ "$unpaid_count" -gt 0 ]]; then
    echo "[$(date -Iseconds)] Unpaid deposits found but alert not sent (not first run or dedup)"
  else
    echo "[$(date -Iseconds)] All deposits found — no alert needed"
  fi

  echo "[$(date -Iseconds)] check_deposits_cron completed"
}

# Lock and run
exec 9>"$LOCKFILE"
if ! flock -n 9; then
  echo "[$(date -Iseconds)] Another instance is already running, skipping"
  exit 0
fi

main "$@" 2>&1 | tee -a "$LOG_DIR/check_deposits_cron.log"