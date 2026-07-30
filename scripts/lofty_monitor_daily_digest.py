#!/usr/bin/env python3
"""Lofty guild monitor daily digest - posts a summary of forwarded messages to #agent-discord-public."""
import json, subprocess, os, sys
from datetime import datetime, timezone, timedelta

TOKEN = os.environ.get("ATLAS_DISCORD_TOKEN", "")
AUTH = "Authorization: Bot " + TOKEN
DIGEST_CHANNEL = "1496921580188467380"
STATE_FILE = "/home/umbrel/.openclaw/workspace/runtime/lofty_monitor_state.json"
MAP_FILE = "/home/umbrel/.openclaw/workspace/config/lofty_channel_monitor_map.json"

# Load the monitor state to see what was forwarded
try:
    with open(STATE_FILE) as f:
        state = json.load(f)
except:
    state = {"lastCheck": {}, "lastRun": None}

last_run = state.get("lastRun", "")
now = datetime.now(timezone.utc)

# Load channel map for address lookup
try:
    with open(MAP_FILE) as f:
        chan_map = json.load(f)
except:
    chan_map = []

chan_id_to_address = {c["channel_id"]: c["full_address"] for c in chan_map}

# Check each channel for messages in the last 24h
# Look for messages that the monitor would have forwarded (relevant ones)
KEYWORDS = [
    "@earl", "earlvanze", "eco systems", "eco system", "the dao", "shareholder",
    "problem", "issue", "broken", "not working", "complaint", "urgent",
    "emergency", "concern", "refund", "sue", "attorney", "lawyer", "legal",
    "atlas", "doug", "@214445717052194817",
]

def is_relevant(content):
    t = content.lower()
    return any(w in t for w in KEYWORDS)

digest_entries = []
BOT_ID = "1473151035211256020"

for chan_id, address in chan_id_to_address.items():
    try:
        r = subprocess.run(
            ["curl", "-s", "-H", AUTH,
             f"https://discord.com/api/v10/channels/{chan_id}/messages?limit=25"],
            capture_output=True, text=True, timeout=10
        )
        msgs = json.loads(r.stdout)
        if not isinstance(msgs, list):
            continue

        for msg in msgs:
            author = msg.get("author", {})
            content = msg.get("content", "")
            if not content.strip():
                continue
            if author.get("bot"):
                continue
            if "Property Forum Thread" in content or "discord.gg/r4aSNhcQd" in content:
                continue

            # Check timestamp - last 24h
            ts = msg.get("timestamp", "")
            if not ts:
                continue
            try:
                msg_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                age_hours = (now - msg_time).total_seconds() / 3600
                if age_hours > 24:
                    continue
            except:
                continue

            if is_relevant(content):
                name = author.get("username", "Unknown")
                msg_id = msg.get("id", "")
                link = f"https://discord.com/channels/847877825373012018/{chan_id}/{msg_id}"
                digest_entries.append({
                    "address": address,
                    "author": name,
                    "content": content[:200],
                    "link": link,
                    "timestamp": ts,
                })
    except:
        pass

# Build the digest message
if not digest_entries:
    digest = "📋 **Daily Lofty Guild Digest**\n\nNo messages requiring attention in the last 24h."
else:
    lines = [f"📋 **Daily Lofty Guild Digest** ({len(digest_entries)} items requiring attention)\n"]
    for entry in digest_entries:
        lines.append(f"**[{entry['address']}]** {entry['author']}:")
        lines.append(f"{entry['content']}")
        lines.append(f"→ <{entry['link']}>")
        lines.append("")
    digest = "\n".join(lines)[:2000]

# Post to #agent-discord-public
try:
    r = subprocess.run(
        ["curl", "-s", "-X", "POST",
         f"https://discord.com/api/v10/channels/{DIGEST_CHANNEL}/messages",
         "-H", AUTH, "-H", "Content-Type: application/json",
         "-d", json.dumps({"content": digest})],
        capture_output=True, text=True, timeout=15
    )
    result = json.loads(r.stdout)
    if "id" in result:
        print(f"OK: Daily digest posted ({len(digest_entries)} items)")
    else:
        print(f"FAIL: {str(result)[:100]}")
except Exception as e:
    print(f"FAIL: {e}")