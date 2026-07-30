# Pipeline Manifest

This repository retains the full reusable Baselane/Lofty operating surface. The primary entry points are below; their sibling scripts are retained because they are called directly or provide policy-gated evidence, reconciliation, review, and recovery support.

| Lane | Canonical entry point | Supporting families | Default effect |
| --- | --- | --- | --- |
| Daily finance sync | `scripts/baselane_cron_run.sh` | `baselane_sync_*`, `baselane_export_*`, `baselane_daily_*`, `baselane_ecogl_*`, `baselane_source_*`, `split_ledger_*`, `sync_ledger_*` | Read/refresh; guarded source fixes only with explicit apply flags. |
| Deposit evidence | `scripts/check_deposits_cron.sh` | `check_deposits_*`, `baselane_hemlane_*`, `hemlane_financial_evidence.py` | Read-only exception reporting. |
| Weekly review | `scripts/baselane_weekly_file_updates_cron.sh` | `baselane_weekly_*`, `baselane_monthly_mortgage_workflow_idempotent.sh`, mortgage evidence/queue scripts, CF review scripts | Review-first; live actions default off. |
| Monthly accruals | `scripts/baselane_monthly_accruals_28th_cron.sh` | `baselane_monthly_accruals_*`, `baselane_apply_monthly_accruals_live.py`, PM-fee checks | Explicitly guarded Baselane accrual lane. |
| Monthly close | `scripts/baselane_financials_monthly_cron.sh` | `baselane_monthly_*`, `baselane_financials_*`, `baselane_live_cf_*`, `lofty_monthly_*`, `lofty_capture_*` | Builds review artifacts; live publication and messages require explicit flags. |
| Statements and P&I | `scripts/baselane_monthly_statements_idempotent.sh` | `baselane_statements_*`, `run_mortgage_statement_downloaders.py`, `mortgage_workflow_*`, `update_coownership_mortgage_tokenomics.py` | Evidence and reconciliation with explicit mutation gates. |
| Yhome/Web3 reconciliation | `scripts/refresh_yhome_transition_reconciliation.py` | `yhome_*`, `baselane_web3_reconciliation_apply.py`, `baselane_fix_yhome_*`, `reconcile_*` | Read/compare first; non-cash entries remain non-cash absent bank evidence. |
| Baselane MCP | `skills/baselane-mcp/src/baselane_mcp/server.py` | `transfers.py`, local MCP scripts, root Baselane scripts | Guarded local tool API. |

## Retained manual-only scripts

Files named for a specific property, date, payment, or remediation are preserved as an audit/replay record. They are not scheduled, do not create a standing policy, and must be run only under the live-action protocol in `AGENTS.md`. Before reuse, confirm the transaction IDs, property scope, accounting treatment, and target state still match live data.

## Configuration boundary

The checked-in `config/*.json` files encode mapping/classification and approved accounting decisions. They are part of production behavior. Runtime paths, auth state, browser/CDP endpoints, and secret material are local-only and intentionally absent.

## Session boundary

Legacy credential bootstrap, token extraction, cookie seeding, MFA handling, CAPTCHA handling, and browser-login helpers are intentionally not included. Every live lane begins only after a human has established an authorized visible browser session; `baselane_cdp_auth_recovery.py` is a read-only availability check despite its legacy-compatible filename.
