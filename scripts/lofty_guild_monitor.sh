#!/usr/bin/env bash
# Lofty Guild Property Channel Monitor (parallel version)
# Polls all Lofty guild property channels in parallel, classifies and routes messages.
# Only forwards messages relevant to Earl/ECO Systems to the matching forum thread.
# Run via cron every 5 minutes.

set -euo pipefail

WORKSPACE="/home/umbrel/.openclaw/workspace"
MAP_FILE="$WORKSPACE/config/lofty_channel_monitor_map.json"
STATE_FILE="$WORKSPACE/runtime/lofty_monitor_state.json"
FALLBACK_CHANNEL="1496921580188467380"
BOT_ID="1473151035211256020"
TOKEN="***"
AUTH="Authorization: Bot ***"
PROCESSOR="$WORKSPACE/scripts/lofty_monitor_process.py"
TMPDIR=$(mktemp -d)

export BT="$TOKEN"

mkdir -p "$(dirname "$STATE_FILE")"

if [ ! -f "$STATE_FILE" ]; then
  echo '{"lastCheck":{},"lastRun":null}' > "$STATE_FILE"
fi

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Lofty guild monitor starting..."

# Read channel map and launch parallel curl for each channel
python3 -c "
import json
with open('$MAP_FILE') as f:
    data = json.load(f)
for c in data:
    print(f\"{c['channel_id']}|{c['forum_thread_id']}|{c['full_address']}\")
" | while IFS='|' read -r chan_id forum_tid address; do
  LAST_ID=$(python3 -c "
import json
with open('$STATE_FILE') as f:
    s = json.load(f)
print(s.get('lastCheck',{}).get('$chan_id',''))
" 2>/dev/null || echo "")

  URL="https://discord.com/api/v10/channels/${chan_id}/messages?limit=10"
  [ -n "$LAST_ID" ] && URL="${URL}&after=${LAST_ID}"

  curl -s -H "$AUTH" "$URL" > "$TMPDIR/${chan_id}.json" 2>/dev/null &
done

wait

# Process results in parallel
for f in "$TMPDIR"/*.json; do
  chan_id=$(basename "$f" .json)

  MAPPING=$(python3 -c "
import json
with open('$MAP_FILE') as f:
    data = json.load(f)
for c in data:
    if c['channel_id'] == '$chan_id':
        print(f\"{c['forum_thread_id']}|{c['full_address']}\")
        break
" 2>/dev/null)
  [ -z "$MAPPING" ] && continue
  IFS='|' read -r forum_tid address <<< "$MAPPING"

  if ! python3 -c "import json; json.load(open('$f'))" 2>/dev/null; then
    continue
  fi

  python3 "$PROCESSOR" "$chan_id" "$forum_tid" "$address" "$BOT_ID" "$FALLBACK_CHANNEL" "$STATE_FILE" "$f" 2>/dev/null &
done

wait

rm -rf "$TMPDIR"

python3 -c "
import json
with open('$STATE_FILE') as f:
    s = json.load(f)
s['lastRun'] = '$(date -u +%Y-%m-%dT%H:%M:%SZ)'
with open('$STATE_FILE','w') as f:
    json.dump(s, f, indent=2)
"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Done"