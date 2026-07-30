#!/usr/bin/env node
// Lofty Guild Property Channel Monitor
// Polls Lofty guild property channels for new messages and routes them:
// - Messages requiring attention → matching forum thread in 🏘️-lofty-pm
// - Everything else → #agent-discord-public (1496921580188467380)
//
// Run via cron every 15 minutes

const { execSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const WORKSPACE = '/home/umbrel/.openclaw/workspace';
const MAP_FILE = path.join(WORKSPACE, 'config/lofty_channel_monitor_map.json');
const STATE_FILE = path.join(WORKSPACE, 'runtime/lofty_monitor_state.json');
const FALLBACK_CHANNEL = '1496921580188467380';
const BOT_ID = '1473151035211256020';

function log(msg) {
  console.log(`[${new Date().toISOString()}] ${msg}`);
}

function curlGet(url, token) {
  try {
    const result = execSync(`curl -s -H "Authorization: Bot ${token}" "${url}"`, {
      encoding: 'utf8', timeout: 15000, maxBuffer: 1024*1024
    });
    return JSON.parse(result);
  } catch(e) {
    return { error: e.message };
  }
}

function curlPost(url, token, body) {
  try {
    const data = JSON.stringify(body);
    const result = execSync(`curl -s -X POST -H "Authorization: Bot ${token}" -H "Content-Type: application/json" -d '${data.replace(/'/g, "'\\''")}' "${url}"`, {
      encoding: 'utf8', timeout: 15000, maxBuffer: 1024*1024
    });
    return JSON.parse(result);
  } catch(e) {
    return { error: e.message };
  }
}

function needsAttention(content, author) {
  if (author.bot) return false;
  const t = content.toLowerCase();
  // Direct mentions
  if (t.includes('@earl') || t.includes('earlvanze') || t.includes('eco systems') || t.includes('the dao') || t.includes('shareholder')) return true;
  // Questions
  if (t.includes('?') || t.includes('how do') || t.includes('what is') || t.includes('when will') || t.includes('where is') || t.includes('why') || t.includes('can i') || t.includes('could you') || t.includes('help')) return true;
  // Complaints/issues
  if (t.includes('problem') || t.includes('issue') || t.includes('broken') || t.includes('not working') || t.includes('complaint') || t.includes('urgent') || t.includes('emergency') || t.includes('concern') || t.includes('disappointed') || t.includes('unhappy') || t.includes('refund') || t.includes('sue') || t.includes('attorney') || t.includes('lawyer') || t.includes('legal')) return true;
  // Bot mentions
  if (t.includes('atlas') || t.includes('doug') || t.includes('@') && (t.includes('bot') || t.includes('agent'))) return true;
  return false;
}

function loadState() {
  try { return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')); }
  catch(e) { return { lastCheck: {}, lastRun: null }; }
}

function saveState(state) {
  fs.mkdirSync(path.dirname(STATE_FILE), { recursive: true });
  fs.writeFileSync(STATE_FILE, JSON.stringify(state, null, 2));
}

async function main() {
  log('Lofty guild property channel monitor starting...');

  if (!fs.existsSync(MAP_FILE)) {
    console.error('ERROR: Map file not found: ' + MAP_FILE);
    process.exit(1);
  }
  const channelMap = JSON.parse(fs.readFileSync(MAP_FILE, 'utf8'));
  log(`Monitoring ${channelMap.length} property channels`);

  const token = process.env.ATLAS_DISCORD_TOKEN;
  if (!token) {
    console.error('ERROR: ATLAS_DISCORD_TOKEN not set');
    process.exit(1);
  }

  const state = loadState();
  state.lastRun = new Date().toISOString();

  let forwarded = 0, routed = 0, errors = 0, skipped = 0;

  for (const chan of channelMap) {
    const chanId = chan.channel_id;
    const forumThreadId = chan.forum_thread_id;
    const address = chan.full_address;
    const lastMsgId = state.lastCheck[chanId];

    let url = `https://discord.com/api/v10/channels/${chanId}/messages?limit=10`;
    if (lastMsgId) url += `&after=${lastMsgId}`;

    const msgs = curlGet(url, token);

    if (!Array.isArray(msgs)) {
      if (msgs.message && msgs.message.includes('Missing Access')) {
        skipped++;
      } else {
        errors++;
      }
      continue;
    }

    if (msgs.length === 0) continue;

    // Update last seen
    state.lastCheck[chanId] = msgs[0].id;

    // Process new messages (oldest first)
    for (const msg of msgs.reverse()) {
      const author = msg.author || {};
      const content = msg.content || '';
      if (!content.trim()) continue;
      if (author.bot && author.id === BOT_ID) continue;
      if (content.includes('Property Forum Thread') || content.includes('discord.gg/r4aSNhcQd')) continue;

      const isAttention = needsAttention(content, author);
      const targetChannel = isAttention ? forumThreadId : FALLBACK_CHANNEL;

      const authorName = author.username || author.global_name || 'Unknown';
      const fwd = `**[${address}]** ${authorName}:\n${content.substring(0, 1500)}`;

      const result = curlPost(
        `https://discord.com/api/v10/channels/${targetChannel}/messages`,
        token,
        { content: fwd }
      );

      if (result.id) {
        forwarded++;
        if (isAttention) routed++;
      } else {
        errors++;
      }

      await new Promise(r => setTimeout(r, 500));
    }
  }

  saveState(state);
  log(`Done: ${forwarded} forwarded (${routed} to forum, ${forwarded - routed} to general), ${skipped} no access, ${errors} errors`);
}

main().catch(e => { console.error('ERROR: ' + e.message); process.exit(1); });
