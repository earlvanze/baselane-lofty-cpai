#!/usr/bin/env node
"use strict";

/*
 * Verify one Baselane bank-scoped SMS code without exposing the code or the
 * returned Unit token in argv, stdout, stderr, or a temporary file.
 *
 * The caller supplies the code only in BASELANE_LOCAL_OTP and the bank ID as
 * argv[2]. The authenticated visible Baselane tab remains the source of all
 * browser/session credentials.
 */

const { spawnSync } = require("child_process");
const path = require("path");
const WebSocket = require("ws");

const otp = String(process.env.BASELANE_LOCAL_OTP || "").trim();
const bankId = Number(process.argv[2]);
const requestedTargetId = String(process.env.BASELANE_GQL_TARGET_ID || "").trim();
const workspaceRoot = process.env.OPENCLAW_WORKSPACE_ROOT || "/home/digit/.openclaw/workspace";
const bridge = process.env.BASELANE_GRAPHQL_BRIDGE || path.join(
  workspaceRoot,
  "repos",
  "baselane-lofty-cpai",
  "scripts",
  "baselane_graphql_via_cdp.js",
);
const cdpBase = String(
  process.env.BASELANE_CDP_VERSION_URL || "http://127.0.0.1:19222/json/version",
).replace(/\/json\/version(?:\?.*)?$/, "");

function fail(detail) {
  process.stdout.write(JSON.stringify({
    status: "verification_failed",
    detail,
    sensitive_values_exposed: false,
  }) + "\n");
  process.exit(1);
}

async function writeTokenToVisibleTab(tokenPayload) {
  const response = await fetch(`${cdpBase}/json/list`);
  if (!response.ok) throw new Error("CDP target list unavailable");
  const targets = await response.json();
  const target = targets.find((item) =>
    item.type === "page" &&
    String(item.url || "").startsWith("https://app.baselane.com/") &&
    (!requestedTargetId || item.id === requestedTargetId) &&
    item.webSocketDebuggerUrl
  );
  if (!target) throw new Error("visible Baselane tab unavailable");

  const socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.once("open", resolve);
    socket.once("error", reject);
  });
  try {
    const expiresAt = (Date.now() / 1000) + Number(tokenPayload.expiresIn) - 3600;
    const value = JSON.stringify({
      sensitive: { time: expiresAt, token: tokenPayload.token },
    });
    const expression = `localStorage.setItem(${JSON.stringify(`unitTokenTime_${bankId}`)}, ${JSON.stringify(value)}); true`;
    const reply = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("CDP storage update timed out")), 15000);
      socket.on("message", (raw) => {
        const message = JSON.parse(String(raw));
        if (message.id !== 1) return;
        clearTimeout(timer);
        resolve(message);
      });
      socket.send(JSON.stringify({
        id: 1,
        method: "Runtime.evaluate",
        params: { expression, returnByValue: true },
      }));
    });
    if (reply.error || reply.result?.exceptionDetails || reply.result?.result?.value !== true) {
      throw new Error("Baselane token storage update failed");
    }
  } finally {
    socket.close();
  }
}

async function main() {
  if (!/^\d{6}$/.test(otp)) fail("OTP environment is missing or invalid");
  if (!Number.isSafeInteger(bankId) || bankId <= 0) fail("bank ID is invalid");

  const payload = {
    operationName: "getUserSensitiveTokenData",
    variables: { otpCode: otp, bankId },
    query: "query getUserSensitiveTokenData($otpCode: String!, $bankId: Float!) { unitAPISensitiveToken(otpCode: $otpCode, bankId: $bankId) { expiresIn token } }",
  };
  const child = spawnSync(process.execPath, [bridge, "/dev/stdin"], {
    cwd: path.dirname(path.dirname(path.dirname(bridge))),
    input: JSON.stringify(payload),
    encoding: "utf8",
    env: { ...process.env, OPENCLAW_WORKSPACE_ROOT: workspaceRoot },
    timeout: 90000,
    maxBuffer: 4 * 1024 * 1024,
  });
  if (child.status !== 0) fail("authenticated Baselane verification request failed");

  let body;
  try {
    body = JSON.parse(child.stdout);
  } catch (_) {
    fail("Baselane verification response was unreadable");
  }
  const tokenPayload = body?.data?.unitAPISensitiveToken;
  if (
    body?.errors ||
    !tokenPayload ||
    typeof tokenPayload.token !== "string" ||
    !tokenPayload.token ||
    !Number.isFinite(Number(tokenPayload.expiresIn))
  ) {
    fail("Baselane rejected the bank verification code");
  }

  await writeTokenToVisibleTab(tokenPayload);
  process.stdout.write(JSON.stringify({
    status: "verified",
    bank_id: bankId,
    sensitive_values_exposed: false,
  }) + "\n");
}

main().catch(() => fail("Baselane bank verification handoff failed"));
