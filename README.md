# Baselane → Lofty CPAI

Deterministic operating code for the Baselane-to-Lofty finance pipeline. It keeps the reusable automation, accounting rules, guardrails, and configuration required to reconcile DAO cash, maintain the ECO general ledger, prepare Lofty reporting, and build review artifacts.

This repository intentionally excludes credentials, browser profiles, cookies, persisted MFA material, raw HAR files, bank exports, statements, Google/Dropbox documents, reports, and generated financial data. Approved credentials and one-time codes are supplied only at runtime through guarded helpers and are never committed.

## What is here

| Area | Entry points | Purpose |
| --- | --- | --- |
| Daily | `scripts/baselane_cron_run.sh` | Sync Baselane, validate the canonical ledger, run source-cash checks, and rebuild safe downstream artifacts. |
| Weekly | `scripts/baselane_weekly_file_updates_cron.sh` | Run the idempotent unprocessed-transaction, mortgage, reconciliation, and Lofty review gates. |
| Monthly accruals | `scripts/baselane_monthly_accruals_28th_cron.sh` | Run the guarded 28th PM-fee/DAO-fee accrual lane and its recovery detector. |
| Monthly close | `scripts/baselane_financials_monthly_cron.sh` | Build finance-truth, statement, governance, owner-review, and Lofty review packets. |

OpenClaw can invoke the same canonical control plane through `baselane-mcp`:
`validate_intercompany_overrides`, `refresh_dao_cash_reconciliation`,
`rebuild_monthly_review`, and `get_pipeline_artifact`. These are bounded
operations rather than an arbitrary script runner. The monthly MCP entry point
is always a forced dry run with all cash, external-write, and send switches
disabled.
| Cashflow propagation | `skills/baselane-financials/scripts/update_cf_statements.py` | Propagate the canonical Baselane/ECO GL into each property Cash Flow workbook; those workbooks feed downstream Lofty financial review. |
| No-DAO mortgage liability | `scripts/baselane_reconcile_no_dao_mortgage_liability.py` | Build an exact-ID, read-only waterfall of DAO-paid P&I/fees, restricted escrow, confirmed ECO reimbursements, and unresolved transfer candidates. |
| Alawa ECO transfer allocation | `scripts/baselane_split_alawa_eco_transfers.py` | Preview/apply the approved five-transfer native split plan with exact-ID guards, tag-24-only components, a confirmation digest, and independent readback. |
| Baselane MCP | `skills/baselane-mcp/` | Local MCP surface for exports, guarded splits, reporting, and internal-only transfers. |
| Accounting policies | `config/*.json` | Versioned classification, split, accrual, reserve, Web3-reconciliation, and Lofty-policy decisions. |

`docs/PIPELINE_MANIFEST.md` maps these lanes in more detail. Scripts retained for a dated correction or reconciliation are clearly treated as manual-only; they are never a substitute for the scheduled workflows.

The full data and approval flow is documented in [Workflow](docs/WORKFLOW.md). The optional local-model observer is documented in [Local CPAI control plane](docs/LOCAL_CPAI.md).

## DAO cash and intercompany labels

Investor-facing summaries deliberately separate custody from debt:

- **ECO-held DAO Cash (Gross)** is transaction-backed DAO cash currently in an ECO-controlled bank account.
- **Less: Accrued but Unpaid Obligations** is the recorded restricted portion of that custody cash.
- **ECO Net DAO Funds (spendable)** is the nonnegative remainder available to the DAO after those obligations.
- **Spendable Baselane/ECO cash** is physical custody cash after recorded obligations. The separate Lofty Operating Reserve ledger is shown on its own line and is never combined into another investor-facing balance.
- **DAO A/P - Due to ECO (unreimbursed cash advances)** is the negative side of the ID-bearing ECO-account cash rollforward: actual ECO cash advances less actual DAO cash credits/reimbursements. Its reciprocal ECO receivable remains an internal double-entry control and is not repeated in the DAO summary. Property tags, GL Column E, or a diagnostic shortfall alone cannot establish this balance. Other accrued property obligations are itemized by counterparty and category.
- Historical ECO-account rows with a blank or generic property tag may enter that rollforward only through `config/baselane_intercompany_transaction_overrides.json`. Every rule is bound to the exact Baselane ID, date, amount, account, original property value, target property, classification, rationale, and reciprocal/source evidence. Drift or missing evidence stops the reconciliation; the policy digest and rule count are embedded in its report.
- The **$3,000 co-ownership reserve floor** is an internal control using the full signed Lofty Operating Reserve balance plus Baselane/ECO spendable cash. It does not create an additional investor-facing “reserve-adjusted” balance and is not an additional $3,000 that must remain in an ECO-controlled bank account.
- A monthly investor summary uses one exact reporting cutoff. For the July 2026 close, the cash authority, GL cutoff, summary heading, and publication packet must all be dated **2026-07-31**; an earlier same-month snapshot fails closed as stale.

The property GL Column E total remains an internal accounting control that can contain accruals and non-cash rows. It is not a bank balance, custody balance, or investor-facing cash measure.

Monthly cash-position coverage is generated from
`config/lofty_active_property_roster_policy.json`, the current ownership
schedule, and a fresh Lofty manager snapshot. For the July 2026 close, the
authority is exactly 32 active physical properties and 30 grouped reporting
targets. Selling properties remain active; evidenced sold, closed, delisted,
entity-only, and split-only rows cannot substitute for an active target.

Active Lofty property DAOs accrue the full $750 annual DAO registration/admin
charge as paired $62.50 monthly entries: DAO legal/admin expense and matching
ECO fee revenue. ECO's separate $200 annual Lofty payable and actual
filing/vendor costs are ECO expenses and never reduce the DAO charge. Cash
payments settle the accrued payable without creating another fee.

## Safety model

- Every job uses an authenticated, visible Baselane browser session. Maintained runtime helpers may recover that session and may retrieve a Baselane bank SMS from the authorized Fold 7 through Tailnet-only wireless ADB. The code remains memory-only, is never printed, persisted, or passed in argv, cannot be reused, and the resulting short-lived token is written only to the visible Baselane tab. The repository never stores credentials, codes, cookies, or browser-profile state, and it does not bypass CAPTCHA.
- Start with a preview/dry-run. An apply operation requires its exact confirmation digest/token where the script supports one, then independent verification.
- `transfer_cash` is an idempotent, resumable internal-workspace state machine. It rejects external recipients, wires, ACH send-money, checks, and any tag other than `Transfers Between Accounts` (24); OTP-required means cash definitely did not move, while uncertain submissions fail closed for reconciliation.
- The monthly close runs `baselane_transfer_interest_to_eco.py` after live cash reconciliation. It sweeps only exact DAO savings-interest credits into ECO 2624, preserves security/reserve principal and cash floors, persists confirmation state, and refreshes live cash after any successful transfer.
- No transaction, split, tag, manual row, ledger rewrite, Google Sheet edit, email, Telegram message, Lofty publish, or cash transfer may be applied solely because a balance is negative. Require source evidence and the relevant approval.
- Generated exports and reports are deliberately ignored by Git. Commit code and policy/configuration—not live financial records.

Read [AGENTS.md](AGENTS.md) before making an accounting change.

## Install

Requirements: Python 3.11+, Node.js 20+, `jq`, `curl`, `flock`, and an authorized visible browser session with CDP reachable from the host. Python dependencies are listed in [requirements.txt](requirements.txt); the MCP package also retains its own locked `uv` environment.

```bash
git clone git@github.com:earlvanze/baselane-lofty-cpai.git
cd baselane-lofty-cpai
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
# Optional: use the MCP's locked environment instead of its editable pip install.
uv sync --project skills/baselane-mcp
export OPENCLAW_WORKSPACE_ROOT="$PWD"
export PYTHONPATH="$PWD/skills/baselane-mcp/src:$PWD/scripts"
```

`requirements.txt` is the portable bootstrap manifest for the full script surface: workbook propagation, reviewed HTTP clients, mortgage-PDF parsing, CDP helpers, and the editable Baselane MCP. `skills/baselane-mcp/pyproject.toml` and `uv.lock` remain authoritative for the MCP package's resolved dependency set. Ollama is optional and required only for the local CPAI shadow observer.

## Workspace dependencies

The core cross-repository dependency is [lofty-pm](https://github.com/earlvanze/lofty-pm), used for Lofty property maps, reviewed update payloads, and guarded publication. Install it alongside this repository when running Lofty lanes:

```bash
git clone git@github.com:earlvanze/lofty-pm.git skills/lofty-pm
```

[Baselane MCP](skills/baselane-mcp/) is included in this repository, not a separate clone. It is installed with `uv sync --project skills/baselane-mcp` and exposes the guarded internal-transfer and Baselane workflow tools.

[Cashflow propagation](skills/baselane-financials/SKILL.md) is also included in this repository. It is an in-line pipeline component, not an optional workspace integration: it applies the canonical Baselane/ECO GL to the per-property Cash Flow workbooks that feed downstream Lofty live-financial review. Its hooks run in the weekly statement pass and the property-scoped monthly owner-statement backfill; the daily source-cash audit and several review tools import the same canonical mapping logic.

Create local-only paths and runtime variables in `config/local/`; do not commit them. The checked-in policy JSON files are data-bearing accounting configuration, so changes require a reason and review.

## Operating the pipeline

Use the runbooks:

- [Daily](docs/runbooks/daily.md)
- [Weekly](docs/runbooks/weekly.md)
- [Monthly](docs/runbooks/monthly.md)
- [No-DAO mortgage liability](docs/runbooks/no-dao-mortgage-liability.md)
- [Recovery and mutation controls](docs/runbooks/recovery-and-mutations.md)
- [Entire workflow diagram](docs/WORKFLOW.md)
- [Local CPAI control plane](docs/LOCAL_CPAI.md)

Before a live run, validate the repository itself:

```bash
bash scripts/verify_repo.sh
```

The scheduler example is [config/baselane-lofty.crontab.example](config/baselane-lofty.crontab.example). It leaves communications disabled by default and must be adapted to the local workspace path, logs path, and approved runtime environment.

## Local model (shadow only)

`scripts/cpai_local_supervisor.py` provides a bounded, local Ollama observer for deterministic reports. Its current policy permits `qwen2.5:14b-instruct` only and returns a schema-validated advisory envelope. It never dispatches scripts or approves financial, workbook, or external changes. Run it only after the corresponding deterministic lane has written its reports.

`scripts/cpai_shadow_after_daily.sh` is the non-blocking daily companion. It reads the completed daily reports and writes a separate shadow report; it never changes the daily lane's action policy or dispatches work.

## MCP

```bash
cd skills/baselane-mcp
uv run baselane-mcp
```

Available operations include statement/ledger export, mortgage/batch-split previews, the read-only no-DAO-mortgage liability waterfall, internal account discovery, guarded internal transfers, and a weekly unprocessed report. See [skills/baselane-mcp/SKILL.md](skills/baselane-mcp/SKILL.md).

## Repository maintenance

1. Add a script only when it is reusable, scheduled, or necessary to verify an existing workflow.
2. Put dated one-off repairs behind explicit preview/apply/verify guards and label them manual-only in the source docstring.
3. Never commit live exports, receipt images, documents, HAR files, secrets, or browser state.
4. Run `bash scripts/verify_repo.sh`, inspect `git diff --check`, then commit a focused change.
5. Keep `README.md`, `AGENTS.md`, the manifest, and the relevant runbook in sync when changing an entry point or a financial invariant.
