---
name: baselane-mcp
description: Use the canonical Baselane MCP and Baselane-to-Lofty CPAI workflows for authenticated reads, guarded internal transfers, native splits, ledger exports, bank-SMS recovery through an authorized Fold 7, reconciliation, and finance-pipeline operations.
---

# Baselane MCP

Local MCP server for guarded Baselane finance operations through a visible browser session reachable by CDP. It stores no credentials, MFA codes, cookies, or browser profiles. A deterministic runtime helper may retrieve and submit a fresh Baselane bank SMS from the authorized Fold 7 without exposing or persisting the code.

## Tools

| Tool | Purpose | Live-action guard |
| --- | --- | --- |
| `get_auth_status` | Read-only attached-session check | No mutation |
| `export_statements`, `export_ledger`, `weekly_unprocessed_report` | Read/export finance evidence | `dry_run=true` by default |
| `split_mortgage`, `batch_split` | Preview supported split workflows | Explicit `dry_run=false` after review |
| `reconcile_no_dao_mortgage_liability` | Exact-ID P&I/fee/escrow/ECO reimbursement waterfall | Read-only; candidates never settle balances automatically |
| `split_alawa_eco_transfers` | Preview/apply the five exact-ID ECO→Alawa native transfer allocations | Apply requires the fresh preview digest; every child remains tag 24 |
| `validate_intercompany_overrides` | Validate exact override rules against the current ID-bearing source index | Local read-only evidence; source or policy drift blocks |
| `refresh_dao_cash_reconciliation` | Rebuild canonical custody, restriction, and verified DAO A/P artifacts | Live Baselane reads only; no financial mutation |
| `rebuild_monthly_review` | Run the canonical monthly review pipeline for a cutoff | Forced dry run; all cash, sheet, publication, message, and email switches are off |
| `get_pipeline_artifact` | Inspect an allowlisted downstream JSON artifact or one property's records | Read-only; arbitrary paths are rejected |
| `list_transfer_accounts` | List eligible internal accounts with masked numbers | No mutation |
| `get_transfer_status` | Inspect one durable confirmation-token state before resuming | No mutation |
| `transfer_cash` | Move cash within one Baselane workspace | Exact dry-run confirmation token; internal accounts only; tag 24 only |

## Run

```bash
cd skills/baselane-mcp
uv run baselane-mcp
```

Set `OPENCLAW_WORKSPACE_ROOT` to the repository root and ensure `PYTHONPATH` includes `skills/baselane-mcp/src` and `scripts`.

Always preview first. For a transfer, confirm the source/destination accounts, property ID, label, date, amount, and confirmation token; then independently reconcile both post-transfer mirrors. Never use the MCP to send to an external recipient.

For normal OpenClaw accounting work, validate policy before rebuilding live
cash, then inspect the resulting property record. Use the monthly review tool
only when the full guarded artifact chain is needed:

```bash
mcporter call baselane.validate_intercompany_overrides as_of="2026-07-31"
mcporter call baselane.refresh_dao_cash_reconciliation as_of="2026-07-31"
mcporter call baselane.get_pipeline_artifact artifact="dao_cash_reconciliation" property_name="90 Madison Ave"
mcporter call baselane.rebuild_monthly_review run_month="2026-07" reporting_cutoff_date="2026-07-31"
```

The rebuild tool never accepts arbitrary commands or paths. Its environment
forces every financial apply, Google Sheet write, Lofty/Discord/Telegram
publication, and owner-email switch off even if the parent OpenClaw process has
those variables enabled.

## Guarded transfer state machine

Preview once, then apply the exact same inputs and confirmation token. On an
OTP challenge, `transfer_cash` defaults to `auto_mfa=true`: it discovers the
single already-authorized Fold 7 Tailnet ADB connection, retrieves a fresh
Baselane SMS in memory, verifies the source account's parent bank, and retries
the same idempotent transfer. Do not issue a second transfer or change the
confirmation token while this runs.

| Status | Cash interpretation | Next action |
| --- | --- | --- |
| `dry_run` | No cash moved | Review and apply exact token |
| `authentication_required` / `mfa_pending` | Baselane rejected before cash moved | Restore Fold connection or fresh SMS, then retry exact token |
| `completed` / `already_completed` | Cash moved once | Independently verify both mirrors |
| `reconciliation_required` | Outcome may be uncertain | Do not retry; inspect upstream and `get_transfer_status` |

Every listed subaccount includes both `bank_account_id` and
`mfa_bank_account_id`; the latter is the parent bank ID Baselane expects for
OTP. The standalone resend resolver is never the primary transfer path.

### Monthly DAO savings-interest sweep

DAO-bank savings interest is ECO revenue, but only an exact, source-identified
Baselane interest credit may be swept. Run
`scripts/baselane_transfer_interest_to_eco.py` from the current
`baselane_live_dao_cash_reconciliation.json` and ID-bearing
`baselane_source_transaction_index.csv`. The script transfers internally to
ECO 2624, tags both mirrors as `Transfers Between Accounts` (24), preserves
documented security/reserve principal and active-property cash floors, and
persists exact confirmation tokens for idempotency. Never infer interest from
an account balance, a loan-interest label, or a GL subtotal.

The monthly close calls this workflow after live DAO cash reconciliation. A
review run writes a digest-bound plan only. A live-approved monthly run applies
that exact plan, uses the authorized Fold helper for one bank-SMS challenge,
and then rebuilds live cash reconciliation so downstream summaries cannot use
pre-sweep balances. An authentication challenge means no cash moved and the
same exact token is retry-safe; an uncertain submission requires reconciliation
and must never be retried automatically.

The scheduled internal interest sweep has standing owner authorization and is
separate from publication/email approval. `DRY_RUN=1` forces a full rehearsal;
`APPLY_MONTHLY_DAO_INTEREST_SWEEP_LIVE=0` retains planning but disables apply.

## Fold 7 bank-SMS recovery

Use this maintained path only when Baselane requests a bank-scoped SMS sent to the authorized Fold 7:

```bash
cd /home/digit/.openclaw/workspace/repos/baselane-lofty-cpai
python3 scripts/baselane_fold7_mfa.py --bank-id <mfa-bank-account-id>
```

If exactly one authorized Fold 7 Tailnet target is already present in `adb
devices -l`, the helper discovers it. Otherwise set `FOLD7_ADB_TARGET` to its
Tailnet host and current wireless-debugging port; LAN targets are rejected.

First run the already-previewed transfer once. Baselane's authorized transfer
flow requests the bank code when it returns the guarded OTP-required state. If
no fresh code arrives and the current user has access to the standalone bank
verification resolver, request exactly one through the authenticated visible
session, then run the receiver:

```bash
node scripts/baselane_request_bank_otp.js <baselane-bank-id>
```

When more than one authenticated Baselane tab exists, set `BASELANE_GQL_TARGET_ID` to the exact visible CDP target for both the request and receiver commands. The GraphQL bridge then uses a target-specific session cache and the verifier writes the bank token back to that same tab.

The target must be a Tailnet address and the connected model must match the configured Fold 7. The helper queries only fresh Baselane messages, prevents message reuse, supplies the code to the local verifier only through its environment, and never prints or stores the code. On `verified`, resume the already-previewed operation. A standalone request may return `UNAUTHORIZED_ACCESS` for a collaborator who is still authorized to execute the transfer; that result describes only the resend resolver and must not be reported as missing transfer permission. On `no_fresh_code`, preserve the exact transfer digest and retry the operation's own challenge path before considering the optional request helper. On device, model, submission, or unsupported-challenge failure, preserve the pending operation and stop live mutations.

For monthly DAO registration/admin fees, use the canonical accrual lane and
`config/dao_llc_admin_fee_policy.json`. Every active Lofty property DAO owes
$750 per annual service cycle, recognized as paired $62.50 monthly DAO-expense
and ECO-revenue rows. ECO's $200 annual Lofty payable and actual filing/vendor
costs are separate ECO expenses; they never reduce the DAO fee. Cash settlement
clears the accrued payable and must not create a second fee.

For a no-DAO-mortgage property, run the liability reconciler before proposing a transfer or publishing a cash summary:

```bash
mcporter call baselane.reconcile_no_dao_mortgage_liability property_name="85-104 Alawa Pl"
```

The report is authoritative only for its exact source digest. It treats mortgage escrow as restricted DAO cash, treats principal/interest and lender late/return fees paid from the DAO bank as due from ECO, and leaves unlabeled or composite inbound transfers as review candidates until their purpose is explicitly supported.

For the approved Alawa transfer allocation, preview first and pass the returned digest unchanged to apply:

```bash
mcporter call baselane.split_alawa_eco_transfers apply=false
mcporter call baselane.split_alawa_eco_transfers apply=true confirmation_digest="<preview digest>"
```

The apply path verifies the five parent IDs, dates, amounts, counterparties, category 24, target sums, and native-child readback. Purpose/month lives in the component label; it never changes a transfer child into revenue or expense.

## GraphQL transport

The MCP issues direct GraphQL requests after acquiring short-lived session and
Firebase App Check headers from the authorized visible browser. Fresh headers
are reused from a mode-0600 runtime cache; raw CDP is the normal refresh path.
Playwright is disabled by default and may be enabled for manual recovery only
with `BASELANE_GQL_PLAYWRIGHT_FALLBACK=1`.

Use `run_graphql_batch_via_cdp` for bounded multi-page reads so one bridge
process serves the batch. Guarded mutations still require workflow-specific
preconditions and independent exact-ID readback.
