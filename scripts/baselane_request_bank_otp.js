#!/usr/bin/env node
"use strict";

/* Request one bank-scoped Baselane SMS through the authenticated visible tab. */

const { spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const bankId = Number(process.argv[2]);
const workspaceRoot = process.env.OPENCLAW_WORKSPACE_ROOT || "/home/digit/.openclaw/workspace";
const bridge = process.env.BASELANE_GRAPHQL_BRIDGE || path.join(
  workspaceRoot,
  "repos",
  "baselane-lofty-cpai",
  "scripts",
  "baselane_graphql_via_cdp.js",
);

function finish(status, detail, exitCode) {
  process.stdout.write(JSON.stringify({
    status,
    bank_id: Number.isSafeInteger(bankId) ? bankId : null,
    detail,
    sensitive_values_exposed: false,
  }) + "\n");
  process.exit(exitCode);
}

if (!Number.isSafeInteger(bankId) || bankId <= 0) {
  finish("request_failed", "bank ID is invalid", 2);
}

const payload = {
  operationName: "getOTP",
  variables: { bankId },
  query: "query getOTP($bankId: Float!) { unitAPIVerification(bankId: $bankId) }",
};
const requestDir = fs.mkdtempSync(path.join(os.tmpdir(), "baselane-otp-request-"));
const requestPath = path.join(requestDir, "request.json");
let child;
try {
  fs.writeFileSync(requestPath, JSON.stringify(payload), { encoding: "utf8", mode: 0o600 });
  child = spawnSync(process.execPath, [bridge, requestPath], {
    cwd: path.dirname(path.dirname(bridge)),
    encoding: "utf8",
    env: { ...process.env, OPENCLAW_WORKSPACE_ROOT: workspaceRoot },
    timeout: 90000,
    maxBuffer: 4 * 1024 * 1024,
  });
} finally {
  try { fs.rmSync(requestDir, { recursive: true, force: true }); } catch (_) {}
}
if (child.status !== 0) {
  if (/UNAUTHORIZED_ACCESS|Unauthorized access to bank id/i.test(String(child.stderr || ""))) {
    finish(
      "request_unavailable",
      "standalone SMS resend is unavailable for the current user; use the reviewed transfer challenge",
      4,
    );
  }
  finish("request_failed", "authenticated Baselane OTP request failed", 3);
}

let body;
try {
  body = JSON.parse(child.stdout);
} catch (_) {
  finish("request_failed", "Baselane OTP response was unreadable", 3);
}
if (body?.errors || body?.data?.unitAPIVerification !== true) {
  finish("request_failed", "Baselane did not confirm SMS delivery", 3);
}
finish("requested", "fresh Baselane bank SMS requested", 0);
