#!/usr/bin/env python3
"""Lofty guild monitor message processor - called by lofty_guild_monitor.sh"""
import json, subprocess, os, sys, time

token = os.environ["BT"]
auth = "Authorization: Bot " + token
chan_id = sys.argv[1]
forum_tid = sys.argv[2]
address = sys.argv[3]
bot_id = sys.argv[4]
fallback = sys.argv[5]
state_file = sys.argv[6]
msgs_file = sys.argv[7]
LOFTY_GUILD = "847877825373012018"

with open(msgs_file) as fh:
    msgs = json.load(fh)

if not isinstance(msgs, list) or not msgs:
    sys.exit(0)

# Update last seen
first_id = msgs[0]["id"]
try:
    with open(state_file) as fh:
        state = json.load(fh)
except:
    state = {"lastCheck": {}}
state.setdefault("lastCheck", {})[chan_id] = first_id
with open(state_file, "w") as fh:
    json.dump(state, fh, indent=2)

KEYWORDS = [
    "@earl", "earlvanze", "eco systems", "eco system", "the dao", "shareholder",
    "problem", "issue", "broken", "not working", "complaint", "urgent",
    "emergency", "concern", "refund", "sue", "attorney", "lawyer", "legal",
    "atlas", "doug", "@214445717052194817",
]

def needs_attention(content):
    t = content.lower()
    return any(w in t for w in KEYWORDS)

def build_context(msgs_list, target_idx, chan_id):
    lines = []
    start = max(0, target_idx - 2)
    end = min(len(msgs_list), target_idx + 3)
    for i in range(start, end):
        m = msgs_list[i]
        a = m.get("author", {})
        name = a.get("username", "Unknown")
        content = (m.get("content", "") or "").replace("\n", " ").strip()
        if not content:
            continue
        mid = m.get("id", "")
        link = "https://discord.com/channels/" + LOFTY_GUILD + "/" + chan_id + "/" + mid
        marker = " >>> " if i == target_idx else "     "
        lines.append(marker + "**" + name + "**: " + content[:300])
    return "\n".join(lines)

# Process messages (reversed = oldest first)
reversed_msgs = list(reversed(msgs))
for idx, msg in enumerate(reversed_msgs):
    author = msg.get("author", {})
    content = msg.get("content", "")
    if not content.strip():
        continue
    if author.get("bot") and author.get("id") == bot_id:
        continue
    if "Property Forum Thread" in content or "discord.gg/r4aSNhcQd" in content:
        continue
    if author.get("bot"):
        continue

    if not needs_attention(content):
        continue

    name = author.get("username", "Unknown")
    msg_id = msg.get("id", "")
    orig_link = "https://discord.com/channels/" + LOFTY_GUILD + "/" + chan_id + "/" + msg_id
    context = build_context(reversed_msgs, idx, chan_id)
    fwd = "**[" + address + "]**\n\n```\n" + context + "\n```\n\n-> <" + orig_link + ">"

    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "POST",
             "https://discord.com/api/v10/channels/" + forum_tid + "/messages",
             "-H", auth, "-H", "Content-Type: application/json",
             "-d", json.dumps({"content": fwd[:2000]})],
            capture_output=True, text=True, timeout=10
        )
        r = json.loads(result.stdout)
        if "id" in r:
            print("FORUM | " + address[:40])
    except:
        pass

    time.sleep(0.3)