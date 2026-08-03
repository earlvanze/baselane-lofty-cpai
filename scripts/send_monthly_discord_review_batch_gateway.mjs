#!/usr/bin/env node

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createHash } from "node:crypto";
import { pathToFileURL } from "node:url";

const EARLCOIN_GUILD_ID = "1473153860376858756";
const EARLCOIN_GUILD_NAME = "EARLCoin";
const EARLCOIN_FORUM_ID = "1480241103528530141";
const EARLCOIN_FORUM_NAME = "eco-systems-pm";
const EARLCOIN_TARGET = `channel:${EARLCOIN_FORUM_ID}`;
const LOFTY_GUILD_ID = "847877825373012018";
const REVIEW_ACCOUNT = "default";
const REVIEW_PREFIX = "[DRAFT FOR REVIEW - NOT EMAILED]\n\n";
const SUCCESS_STATUSES = new Set(["ok", "ok_previous", "ok_dry_run"]);
const EXPECTED_ACTIVE_PHYSICAL_PROPERTY_COUNT = 32;
const EXPECTED_ACTIVE_REPORTING_TARGET_COUNT = 30;

function parseArgs(argv) {
  const args = {};
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === "--send" || value === "--dry-run") {
      args[value.slice(2).replaceAll("-", "_")] = true;
      continue;
    }
    if (!value.startsWith("--") || index + 1 >= argv.length) {
      throw new Error(`Invalid argument: ${value}`);
    }
    args[value.slice(2).replaceAll("-", "_")] = argv[index + 1];
    index += 1;
  }
  for (const required of ["plan", "plan_validation", "thread_inventory", "report"]) {
    if (!args[required]) throw new Error(`Missing --${required.replaceAll("_", "-")}`);
  }
  args.account ??= REVIEW_ACCOUNT;
  args.dry_run = Boolean(args.dry_run || !args.send);
  return args;
}

function readJson(filePath) {
  const value = JSON.parse(fs.readFileSync(filePath, "utf8"));
  if (!value || Array.isArray(value) || typeof value !== "object") {
    throw new Error(`${filePath} did not contain a JSON object`);
  }
  return value;
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function normalizeThreadName(value) {
  return String(value ?? "")
    .trim()
    .replace(/\s+Public\s*$/i, "")
    .toLowerCase()
    .replaceAll("&", "and")
    .replace(/[^a-z0-9]+/g, " ")
    .trim()
    .replace(/\s+/g, " ");
}

function monthLabel(runMonth) {
  const match = /^(\d{4})-(\d{2})$/.exec(String(runMonth ?? ""));
  if (!match) return String(runMonth || "Monthly");
  return new Intl.DateTimeFormat("en-US", { month: "long", year: "numeric", timeZone: "UTC" }).format(
    new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, 1)),
  );
}

function financialReviewBlockers(plan, record) {
  const values = [];
  if (Number(plan.global_financial_review_issue_count || 0) > 0) {
    values.push(...(plan.financial_review_issues ?? []));
  }
  if (record.financial_review_blocked === true) {
    values.push(...(record.financial_review_blockers ?? []));
  }
  return [...new Set(values.map((value) => String(value ?? "").replace(/\s+/g, " ").trim()).filter(Boolean))];
}

function reviewHeader(runMonth, propertyName, blockers) {
  let blockerNote = "";
  if (blockers.length) {
    let preview = blockers.slice(0, 3).map((blocker) => blocker.slice(0, 180)).join("; ");
    if (blockers.length > 3) preview += `; +${blockers.length - 3} more in the dispatch receipt`;
    blockerNote =
      ` Financial readiness remains on hold (${blockers.length} blocker(s)): ${preview}. ` +
      "The draft is being posted so the operator can review it; this is not publication approval.";
  }
  return (
    `${monthLabel(runMonth)} close draft for ${String(propertyName ?? "").trim()} is ready for review.` +
    blockerNote +
    " Reply with edits. Owner email and Lofty guild financial-summary publication each require separate human approval. " +
    "No owner email or Lofty guild summary has been sent."
  );
}

function expectedDestination() {
  return {
    destination_class: "earlcoin_operator_review",
    destination_purpose: "operator_review",
    publication_state: "draft_review_only",
    guild_id: EARLCOIN_GUILD_ID,
    guild_name: EARLCOIN_GUILD_NAME,
    forum_id: EARLCOIN_FORUM_ID,
    forum_name: EARLCOIN_FORUM_NAME,
    target: EARLCOIN_TARGET,
    discord_account_id: REVIEW_ACCOUNT,
    human_approval_required_for_lofty_publication: true,
    lofty_publication_approval_scope: "lofty_guild_financial_summary_publish",
    lofty_publication_guild_id: LOFTY_GUILD_ID,
  };
}

function validateInputs(plan, validation, inventory, account) {
  const issues = [];
  const destination = expectedDestination();
  const activePropertyCount = Number(plan.authoritative_active_property_count || 0);
  const expectedReportingTargetCount = Number(plan.authoritative_reporting_target_count || 0);
  if (!Number.isInteger(activePropertyCount) || activePropertyCount !== EXPECTED_ACTIVE_PHYSICAL_PROPERTY_COUNT) {
    issues.push(
      `authoritative_active_property_count_invalid:${activePropertyCount}:expected=${EXPECTED_ACTIVE_PHYSICAL_PROPERTY_COUNT}`,
    );
  }
  if (
    !Number.isInteger(expectedReportingTargetCount) ||
    expectedReportingTargetCount !== EXPECTED_ACTIVE_REPORTING_TARGET_COUNT
  ) {
    issues.push(
      `authoritative_reporting_target_count_invalid:${expectedReportingTargetCount}:expected=${EXPECTED_ACTIVE_REPORTING_TARGET_COUNT}`,
    );
  }
  if (account !== REVIEW_ACCOUNT) issues.push(`account_mismatch:${account}`);
  for (const [field, expected] of Object.entries(destination)) {
    if (plan[field] !== expected) issues.push(`plan.${field}_mismatch`);
    if (plan.review_destination?.[field] !== expected) issues.push(`plan.review_destination.${field}_mismatch`);
  }
  const reviewReady =
    ["ok", "ok_partial"].includes(plan.status) ||
    (plan.status === "review" &&
      validation.discord_review_ready === true &&
      validation.earlcoin_review_route_ok === true &&
      Number(validation.unmapped_count || 0) === 0 &&
      Number(validation.stale_route_count || 0) === 0 &&
      Number(validation.missing_financial_summary_count || 0) === 0);
  if (!reviewReady) issues.push(`plan_not_review_ready:${plan.status}`);
  if (
    inventory.status !== "ok" ||
    Number(inventory.channel_count || 0) !== expectedReportingTargetCount
  ) {
    issues.push("thread_inventory_not_verified");
  }

  const channels = new Map();
  for (const channel of inventory.channels ?? []) {
    const normalized = normalizeThreadName(channel.property_name);
    if (!normalized || channels.has(normalized)) {
      issues.push(`thread_inventory_duplicate_or_invalid:${normalized}`);
      continue;
    }
    if (!/^channel:\d{17,25}$/.test(String(channel.target ?? ""))) {
      issues.push(`thread_inventory_target_invalid:${channel.property_name}`);
      continue;
    }
    channels.set(normalized, channel);
  }

  const prepared = [];
  const seenThreads = new Set();
  const records = Array.isArray(plan.records) ? plan.records : [];
  if (records.length !== expectedReportingTargetCount) {
    issues.push(`record_count_invalid:${records.length}:expected=${expectedReportingTargetCount}`);
  }
  for (let index = 0; index < records.length; index += 1) {
    const record = records[index];
    const propertyName = String(record?.property_name ?? "").trim();
    const normalized = normalizeThreadName(propertyName);
    const message = String(record?.message ?? "");
    const channel = channels.get(normalized);
    if (!propertyName || !normalized || seenThreads.has(normalized)) {
      issues.push(`record_${index}_property_or_thread_invalid`);
      continue;
    }
    seenThreads.add(normalized);
    if (record.thread_name_normalized !== normalized) issues.push(`record_${index}_thread_name_mismatch`);
    for (const [field, expected] of Object.entries(destination)) {
      if (record[field] !== expected) issues.push(`record_${index}.${field}_mismatch`);
    }
    if (!channel) issues.push(`record_${index}_thread_not_found:${propertyName}`);
    if (!record.message_sha256 || sha256(message) !== record.message_sha256) {
      issues.push(`record_${index}_message_digest_mismatch`);
    }
    const body = REVIEW_PREFIX + message;
    if (Buffer.byteLength(body, "utf8") > 2000) issues.push(`record_${index}_body_exceeds_discord_limit`);
    const blockers = financialReviewBlockers(plan, record);
    const header = reviewHeader(plan.run_month, propertyName, blockers);
    const reviewDigest = sha256(
      [EARLCOIN_GUILD_ID, EARLCOIN_FORUM_ID, normalized, header, body].join("\n"),
    );
    prepared.push({
      index,
      property_name: propertyName,
      thread_name: record.thread_name,
      thread_name_normalized: normalized,
      target: channel?.target ?? null,
      thread_id: String(channel?.target ?? "").replace(/^channel:/, ""),
      message_sha256: record.message_sha256,
      financial_review_blocked: blockers.length > 0,
      financial_review_blockers: blockers,
      header,
      body,
      body_bytes: Buffer.byteLength(body, "utf8"),
      review_digest: reviewDigest,
    });
  }
  if (channels.size !== records.length) issues.push(`thread_map_count_mismatch:${channels.size}:${records.length}`);
  return { issues, prepared };
}

function writeJsonAtomic(filePath, payload) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  const temporary = `${filePath}.tmp`;
  fs.writeFileSync(temporary, `${JSON.stringify(payload, null, 2)}\n`);
  fs.renameSync(temporary, filePath);
}

function deterministicIdempotencyKey(reviewDigest, stage) {
  return `monthly-review-${stage}-${reviewDigest}`;
}

function compactReceipt(receipt) {
  if (!receipt || typeof receipt !== "object") return receipt ?? null;
  return {
    ok: receipt.ok ?? null,
    messageId: receipt.messageId ?? receipt.message_id ?? receipt.id ?? receipt.payload?.messageId ?? null,
    channelId: receipt.channelId ?? receipt.channel_id ?? receipt.payload?.channelId ?? null,
    raw: receipt,
  };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const planPath = path.resolve(args.plan);
  const validationPath = path.resolve(args.plan_validation);
  const inventoryPath = path.resolve(args.thread_inventory);
  const reportPath = path.resolve(args.report);
  const planBytes = fs.readFileSync(planPath);
  const planFileSha256 = sha256(planBytes);
  const plan = JSON.parse(planBytes.toString("utf8"));
  const validation = readJson(validationPath);
  const inventory = readJson(inventoryPath);
  const { issues, prepared } = validateInputs(plan, validation, inventory, args.account);
  const destination = expectedDestination();
  const report = {
    generated_at: new Date().toISOString(),
    status: issues.length ? "review" : "running",
    mode: "all_plan_gateway_batch",
    delivery_mode: "earlcoin_operator_review_drafts",
    execution_owner: "agent:discord-public",
    dry_run: args.dry_run,
    account: args.account,
    plan: planPath,
    plan_file_sha256: planFileSha256,
    plan_validation: validationPath,
    thread_inventory: inventoryPath,
    run_month: plan.run_month,
    record_count: prepared.length,
    review_destination: destination,
    ...destination,
    issues,
    results: [],
    owner_email_sent: false,
  };

  let prior = {};
  const resumePath = path.resolve(args.resume_report ?? reportPath);
  if (fs.existsSync(resumePath)) {
    try {
      prior = readJson(resumePath);
    } catch {
      prior = {};
    }
  }
  const priorByDigest = new Map(
    (prior.results ?? [])
      .filter((item) => item?.review_digest)
      .map((item) => [item.review_digest, item]),
  );

  if (issues.length) {
    writeJsonAtomic(reportPath, report);
    process.stdout.write(`${JSON.stringify({ status: report.status, issue_count: issues.length })}\n`);
    return 2;
  }

  if (args.dry_run) {
    report.results = prepared.map((item) => ({
      ...item,
      header: undefined,
      body: undefined,
      status: "ok_dry_run",
      stage: "would_send_header_then_exact_body",
      guild_id: EARLCOIN_GUILD_ID,
      forum_id: EARLCOIN_FORUM_ID,
      forum_target: EARLCOIN_TARGET,
      thread_id: item.thread_id,
      discord_account_id: args.account,
    }));
    report.status = "ok_dry_run";
    report.sent_or_verified_count = prepared.length;
    report.posted_or_verified_property_count = prepared.length;
    report.failed_count = 0;
    report.all_property_review_drafts_posted = false;
    report.discord_all_property_dry_run_verified = true;
    writeJsonAtomic(reportPath, report);
    process.stdout.write(`${JSON.stringify({ status: report.status, count: prepared.length })}\n`);
    return 0;
  }

  const runtimePath =
    process.env.OPENCLAW_GATEWAY_RUNTIME ||
    path.join(os.homedir(), ".local/lib/node_modules/openclaw/dist/plugin-sdk/gateway-runtime.js");
  const { callGatewayFromCli } = await import(pathToFileURL(runtimePath).href);
  const send = async (item, stage, message) => {
    if (sha256(fs.readFileSync(planPath)) !== planFileSha256) {
      throw new Error("plan_changed_during_send");
    }
    const receipt = await callGatewayFromCli(
      "message.action",
      { json: true, timeout: "15000" },
      {
        channel: "discord",
        action: "send",
        params: { to: item.target, accountId: args.account, message },
        accountId: args.account,
        idempotencyKey: deterministicIdempotencyKey(item.review_digest, stage),
      },
    );
    if (receipt?.ok === false) throw new Error(`discord_send_failed:${JSON.stringify(receipt)}`);
    return compactReceipt(receipt);
  };

  for (const item of prepared) {
    const previous = priorByDigest.get(item.review_digest);
    if (previous?.status === "ok" || previous?.status === "ok_previous") {
      report.results.push({ ...previous, status: "ok_previous", index: item.index });
      writeJsonAtomic(reportPath, report);
      continue;
    }
    const result = {
      index: item.index,
      property_name: item.property_name,
      guild_id: EARLCOIN_GUILD_ID,
      forum_id: EARLCOIN_FORUM_ID,
      forum_target: EARLCOIN_TARGET,
      discord_account_id: args.account,
      thread_name: item.thread_name,
      thread_name_normalized: item.thread_name_normalized,
      target: item.target,
      thread_id: item.thread_id,
      financial_review_blocked: item.financial_review_blocked,
      financial_review_blockers: item.financial_review_blockers,
      review_digest: item.review_digest,
      message_sha256: item.message_sha256,
      body_bytes: item.body_bytes,
      status: "running",
      stage: "header",
    };
    report.results.push(result);
    writeJsonAtomic(reportPath, report);
    try {
      if (previous?.header_receipt) {
        result.header_receipt = previous.header_receipt;
      } else {
        result.header_receipt = await send(item, "header", item.header);
      }
      result.stage = "body";
      result.status = "header_sent";
      writeJsonAtomic(reportPath, report);
      result.body_receipt = await send(item, "body", item.body);
      result.stage = "complete";
      result.status = "ok";
    } catch (error) {
      result.status = "failed";
      result.issue = result.stage === "header" ? "earlcoin_review_header_send_failed" : "earlcoin_review_body_send_failed";
      result.error = error instanceof Error ? error.message : String(error);
    }
    report.generated_at = new Date().toISOString();
    writeJsonAtomic(reportPath, report);
  }

  const okCount = report.results.filter((item) => SUCCESS_STATUSES.has(item.status)).length;
  const failedCount = report.results.length - okCount;
  report.status = okCount === prepared.length && failedCount === 0 ? "ok" : "review";
  report.sent_or_verified_count = okCount;
  report.posted_or_verified_property_count = okCount;
  report.failed_count = failedCount;
  report.posted_with_financial_review_blocker_count = report.results.filter(
    (item) => SUCCESS_STATUSES.has(item.status) && item.financial_review_blocked === true,
  ).length;
  report.all_property_discord_review_proof_ok = report.status === "ok";
  report.discord_all_property_live_post_ok = report.status === "ok";
  report.all_property_review_drafts_posted = report.status === "ok";
  report.operator_review_requested = report.status === "ok";
  report.human_approval_received = false;
  report.lofty_publication_approved = false;
  report.owner_email_approved = false;
  report.discord_all_property_owner_email_review_complete = false;
  report.owner_email_sent = false;
  report.completed_at = new Date().toISOString();
  writeJsonAtomic(reportPath, report);
  process.stdout.write(
    `${JSON.stringify({ status: report.status, record_count: prepared.length, posted_or_verified_property_count: okCount, failed_count: failedCount })}\n`,
  );
  return report.status === "ok" ? 0 : 2;
}

process.exitCode = await main();
