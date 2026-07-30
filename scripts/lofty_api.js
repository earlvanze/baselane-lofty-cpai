#!/usr/bin/env node
// Lofty SDK wrapper — replaces LoftyAssist MCP with direct Lofty API calls
// Uses @loftyaicode/sdk for property/trading data and direct API calls for PM/portfolio features

// Resolve SDK from global npm modules or local node_modules
let LoftyClient;
try {
  LoftyClient = require("@loftyaicode/sdk").LoftyClient;
} catch(e) {
  const globalRoot = require("child_process").execSync("npm root -g", {encoding: "utf8"}).trim();
  LoftyClient = require(require("path").join(globalRoot, "@loftyaicode/sdk")).LoftyClient;
}

const fs = require("fs");
const path = require("path");

const API_KEY = process.env.LOFTY_API_KEY || process.env.LOFTYASSIST_API_KEY;
if (!API_KEY) {
  console.error("ERROR: LOFTY_API_KEY not set");
  process.exit(1);
}

const client = new LoftyClient({ apiKey: API_KEY });

// --- Property functions ---

async function getProperty(propertyId) {
  const r = await client.properties.get(propertyId);
  return r.property || r;
}

async function listProperties(opts = {}) {
  const r = await client.properties.list({
    page: opts.page || 1,
    pageSize: opts.pageSize || 50,
    location: opts.market || opts.location,
    propertyType: opts.propertyType,
  });
  return r;
}

async function searchProperties(term) {
  try {
    const r = await client.properties.list({ pageSize: 100 });
    const props = (r.result && r.result.properties) || r.properties || r.result || [];
    const termLower = term.toLowerCase();
    return props.filter(p => {
      const addr = ((p.address_line1 || "") + " " + (p.address_line2 || "") + " " + (p.city || "") + " " + (p.state || "") + " " + (p.market || "") + " " + (p.slug || "")).toLowerCase();
      return addr.includes(termLower) || (p.id || "").toLowerCase().includes(termLower);
    }).slice(0, 10);
  } catch(e) {
    throw new Error("Search failed: " + e.message);
  }
}

async function getOrderBook(propertyId) {
  return await client.properties.getOrderBook(propertyId);
}

async function getPropertyTrades(propertyId) {
  return await client.properties.getTrades(propertyId);
}

// --- Account functions ---

async function getBalance() {
  return await client.account.getBalance();
}

async function getPositions() {
  return await client.account.getPositions();
}

async function getAccountTrades(opts = {}) {
  return await client.account.getTrades(opts);
}

async function getLpPositions() {
  return await client.account.getLpPositions();
}

async function getLpRewards(opts = {}) {
  return await client.account.getLpRewards(opts);
}

// --- Portfolio functions ---

async function getPortfolioSummary() {
  const positions = await client.account.getPositions();
  const balance = await client.account.getBalance();
  const props = positions.positions || positions || [];
  let totalValue = 0;
  let totalCost = 0;
  const perProperty = [];
  for (const p of props) {
    const value = (p.currentValueUsd || p.marketValue || 0);
    const cost = (p.costBasis || p.totalCostBasis || 0);
    totalValue += value;
    totalCost += cost;
    perProperty.push({
      propertyId: p.propertyId || p.lofty_property_id || p.id,
      address: p.address || p.propertyAddress,
      quantity: p.quantity || p.tokenCount,
      currentValue: value,
      costBasis: cost,
      yield: p.cocYieldPercent || p.yield,
    });
  }
  return {
    totalValue,
    totalCost,
    totalGainLoss: totalValue - totalCost,
    propertyCount: props.length,
    properties: perProperty,
    balance,
  };
}

async function getPortfolioHistory(daysBack = 90) {
  const url = "https://api.lofty.ai/prod/portfolio/history?days=" + daysBack;
  const resp = await fetch(url, {
    headers: { "Authorization": "Bearer " + API_KEY, "Accept": "application/json" }
  });
  if (!resp.ok) throw new Error("Portfolio history failed: " + resp.status);
  return await resp.json();
}

async function getOwnedProperties() {
  const positions = await client.account.getPositions();
  const props = positions.positions || positions || [];
  const enriched = [];
  for (const p of props) {
    const pid = p.propertyId || p.lofty_property_id || p.id;
    if (!pid) continue;
    try {
      const detail = await client.properties.get(pid);
      enriched.push({ ...p, property: detail.property || detail });
    } catch(e) {
      enriched.push(p);
    }
  }
  return enriched;
}

// --- PM functions (direct API) ---

async function getPropertyPmUpdates(propertyId) {
  const url = "https://api.lofty.ai/prod/properties/" + propertyId + "/updates";
  const resp = await fetch(url, {
    headers: { "Authorization": "Bearer " + API_KEY, "Accept": "application/json" }
  });
  if (!resp.ok) throw new Error("PM updates failed: " + resp.status);
  return await resp.json();
}

async function getLatestUpdates(date) {
  const url = "https://api.lofty.ai/prod/properties/updates?date=" + (date || "");
  const resp = await fetch(url, {
    headers: { "Authorization": "Bearer " + API_KEY, "Accept": "application/json" }
  });
  if (!resp.ok) throw new Error("Latest updates failed: " + resp.status);
  return await resp.json();
}

async function getPropertyDocuments(propertyId) {
  const url = "https://api.lofty.ai/prod/properties/" + propertyId + "/documents";
  const resp = await fetch(url, {
    headers: { "Authorization": "Bearer " + API_KEY, "Accept": "application/json" }
  });
  if (!resp.ok) throw new Error("Documents failed: " + resp.status);
  return await resp.json();
}

// --- Market functions ---

async function getPlatformStats() {
  const url = "https://api.lofty.ai/prod/public/v1/market/stats";
  const resp = await fetch(url, {
    headers: { "Authorization": "Bearer " + API_KEY, "Accept": "application/json" }
  });
  if (!resp.ok) throw new Error("Platform stats failed: " + resp.status);
  return await resp.json();
}

async function getMarketIndex(days = 90) {
  const url = "https://api.lofty.ai/prod/public/v1/market/index?days=" + days;
  const resp = await fetch(url, {
    headers: { "Authorization": "Bearer " + API_KEY, "Accept": "application/json" }
  });
  if (!resp.ok) throw new Error("Market index failed: " + resp.status);
  return await resp.json();
}

async function getProfiles() {
  const url = "https://api.lofty.ai/prod/public/v1/profiles";
  const resp = await fetch(url, {
    headers: { "Authorization": "Bearer " + API_KEY, "Accept": "application/json" }
  });
  if (!resp.ok) throw new Error("Profiles failed: " + resp.status);
  return await resp.json();
}

async function runScreener(filtersJson, sortJson) {
  const url = "https://api.lofty.ai/prod/public/v1/properties/screener";
  const body = { filters: JSON.parse(filtersJson), sort: sortJson ? JSON.parse(sortJson) : undefined };
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Authorization": "Bearer " + API_KEY, "Content-Type": "application/json", "Accept": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error("Screener failed: " + resp.status);
  return await resp.json();
}

// --- CLI interface ---
const command = process.argv[2];
const arg = process.argv[3];

const commands = {
  "get-property": () => getProperty(arg),
  "list-properties": () => listProperties(JSON.parse(arg || "{}")),
  "search": () => searchProperties(arg),
  "order-book": () => getOrderBook(arg),
  "trades": () => getPropertyTrades(arg),
  "balance": () => getBalance(),
  "positions": () => getPositions(),
  "portfolio-summary": () => getPortfolioSummary(),
  "portfolio-history": () => getPortfolioHistory(parseInt(arg || "90")),
  "owned-properties": () => getOwnedProperties(),
  "pm-updates": () => getPropertyPmUpdates(arg),
  "latest-updates": () => getLatestUpdates(arg),
  "documents": () => getPropertyDocuments(arg),
  "platform-stats": () => getPlatformStats(),
  "market-index": () => getMarketIndex(parseInt(arg || "90")),
  "profiles": () => getProfiles(),
  "screener": () => runScreener(arg, process.argv[4]),
};

async function main() {
  const fn = commands[command];
  if (!fn) {
    console.error("Unknown command: " + command);
    console.error("Available: " + Object.keys(commands).join(", "));
    process.exit(1);
  }
  try {
    const result = await fn();
    console.log(JSON.stringify(result, null, 2));
  } catch(e) {
    console.error("ERROR: " + e.message);
    process.exit(1);
  }
}

main();