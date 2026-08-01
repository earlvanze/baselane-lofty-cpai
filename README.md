# Baselane → Lofty CPAI

Deterministic operating code for the Baselane-to-Lofty finance pipeline. It keeps the reusable automation, accounting rules, guardrails, and configuration required to reconcile DAO cash, maintain the ECO general ledger, prepare Lofty reporting, and build review artifacts.

This repository intentionally excludes credentials, browser profiles, cookies, MFA material, raw HAR files, bank exports, statements, Google/Dropbox documents, reports, and generated financial data. Those belong in approved local storage and are supplied at runtime.

## What is here

| Area | Entry points | Purpose |
| --- | --- | --- |
| Daily | `scripts/baselane_cron_run.sh` | Sync Baselane, validate the canonical ledger, run source-cash checks, and rebuild safe downstream artifacts. |
| Weekly | `scripts/baselane_weekly_file_updates_cron.sh` | Run the idempotent unprocessed-transaction, mortgage, reconciliation, and Lofty review gates. |
| Monthly accruals | `scripts/baselane_monthly_accruals_28th_cron.sh` | Run the guarded 28th PM-fee/DAO-fee accrual lane and its recovery detector. |
| Monthly close | `scripts/baselane_financials_monthly_cron.sh` | Build finance-truth, statement, governance, owner-review, and Lofty review packets. |
| Cashflow propagation | `skills/baselane-financials/scripts/update_cf_statements.py` | Propagate the canonical Baselane/ECO GL into each property Cash Flow workbook; those workbooks feed downstream Lofty financial review. |
| Baselane MCP | `skills/baselane-mcp/` | Local MCP surface for exports, guarded splits, reporting, and internal-only transfers. |
| Accounting policies | `config/*.json` | Versioned classification, split, accrual, reserve, Web3-reconciliation, and Lofty-policy decisions. |

`docs/PIPELINE_MANIFEST.md` maps these lanes in more detail. Scripts retained for a dated correction or reconciliation are clearly treated as manual-only; they are never a substitute for the scheduled workflows.

The full data and approval flow is documented in [Workflow](docs/WORKFLOW.md). The optional local-model observer is documented in [Local CPAI control plane](docs/LOCAL_CPAI.md).

## DAO cash and intercompany labels

Investor-facing summaries deliberately separate custody from debt:

- **ECO-held DAO Cash (Gross)** is transaction-backed DAO cash currently in an ECO-controlled bank account.
- **Less: Accrued but Unpaid Obligations** is the recorded restricted portion of that custody cash.
- **ECO Net DAO Funds (spendable)** is the nonnegative remainder available to the DAO after those obligations.
- **DAO A/P - Due to ECO for Verified Advances** is cash ECO paid for the DAO in excess of DAO cash credits. It is simultaneously **ECO A/R - Due from DAO** and is never presented as negative cash.
- The **$3,000 co-ownership reserve floor** is one combined test: positive Lofty Operating Reserve plus ECO Net DAO Funds (spendable). It is not an additional $3,000 that must remain in an ECO-controlled bank account.

The property GL Column E total remains an internal accounting control that can contain accruals and non-cash rows. It is not a bank balance, custody balance, or investor-facing cash measure.

## Safety model

- Every job starts from an already authenticated, visible Baselane browser session. This repository does not store or automate credentials, MFA codes, CAPTCHA solving, cookies, or browser-profile state.
- Start with a preview/dry-run. An apply operation requires its exact confirmation digest/token where the script supports one, then independent verification.
- `transfer_cash` is internal-workspace only. It rejects external recipients, wires, ACH send-money, checks, and any tag other than `Transfers Between Accounts` (24).
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

Available operations include statement/ledger export, mortgage/batch-split previews, internal account discovery, guarded internal transfers, and a weekly unprocessed report. See [skills/baselane-mcp/SKILL.md](skills/baselane-mcp/SKILL.md).

## Repository maintenance

1. Add a script only when it is reusable, scheduled, or necessary to verify an existing workflow.
2. Put dated one-off repairs behind explicit preview/apply/verify guards and label them manual-only in the source docstring.
3. Never commit live exports, receipt images, documents, HAR files, secrets, or browser state.
4. Run `bash scripts/verify_repo.sh`, inspect `git diff --check`, then commit a focused change.
5. Keep `README.md`, `AGENTS.md`, the manifest, and the relevant runbook in sync when changing an entry point or a financial invariant.
