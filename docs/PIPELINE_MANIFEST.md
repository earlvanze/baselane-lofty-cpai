# Pipeline Manifest

This repository retains the full reusable Baselane/Lofty operating surface. The primary entry points are below; their sibling scripts are retained because they are called directly or provide policy-gated evidence, reconciliation, review, and recovery support.

| Lane | Canonical entry point | Supporting families | Default effect |
| --- | --- | --- | --- |
| Daily finance sync | `scripts/baselane_cron_run.sh` | `baselane_sync_*`, `baselane_export_*`, `baselane_daily_*`, `baselane_ecogl_*`, `baselane_source_*`, `split_ledger_*`, `sync_ledger_*` | Read/refresh; refreshes both the canonical ledger and digest-verified ID-bearing source index, and fails closed if source-index proof is missing; guarded source fixes only with explicit apply flags. |
| Deposit evidence | `scripts/check_deposits_cron.sh` | `check_deposits_*`, `baselane_hemlane_*`, `hemlane_financial_evidence.py` | Read-only exception reporting. |
| Weekly review | `scripts/baselane_weekly_file_updates_cron.sh` | `baselane_weekly_*`, `baselane_monthly_mortgage_workflow_idempotent.sh`, mortgage evidence/queue scripts, CF review scripts, `skills/baselane-financials` | Review-first; syncs closed-period Cash Flow workbooks before downstream Lofty review. |
| Monthly accruals | `scripts/baselane_monthly_accruals_28th_cron.sh` | `baselane_monthly_accruals_*`, `baselane_apply_monthly_accruals_live.py`, `config/dao_llc_admin_fee_policy.json`, PM-fee checks | Explicitly guarded paired accrual lane; active property DAOs recognize the full $750 annual admin fee at $62.50/month while ECO's Lofty payable and filing costs remain separate ECO expenses. |
| Monthly close | `scripts/baselane_financials_monthly_cron.sh` | `baselane_monthly_*`, `baselane_financials_*`, `baselane_live_cf_*`, `baselane_transfer_interest_to_eco.py`, `config/baselane_intercompany_transaction_overrides.json`, `lofty_monthly_*`, `lofty_capture_*`, `national-grid-statements`, `national_grid_bill_analyzer.py` | Reconciles ECO custody and verified DAO A/P, fail-closes exact historical property corrections against reciprocal/source evidence, plans or applies exact DAO savings-interest sweeps to ECO with principal/floor guards, refreshes cash after mutation, then builds guarded owner-review artifacts and National Grid/APG alerts; live publication/messages require explicit flags. A failed cash reconciliation stops downstream summaries rather than consuming a stale report. |
| OpenClaw/MCP review control plane | `skills/baselane-mcp/src/baselane_mcp/pipeline.py` | `baselane_validate_intercompany_policy.py`, `baselane_live_dao_cash_reconciliation.py`, `baselane_financials_monthly_cron.sh`, allowlisted JSON reports | Exposes policy validation, live read-only cash refresh, forced-dry-run monthly review rebuild, and bounded artifact inspection. It cannot execute arbitrary scripts or enable cash/external writes. |
| Statements and P&I | `scripts/baselane_monthly_statements_idempotent.sh` | `baselane_statements_*`, `run_mortgage_statement_downloaders.py`, `mortgage_workflow_*`, `update_coownership_mortgage_tokenomics.py`, `skills/baselane-financials` | Evidence and reconciliation with explicit mutation gates; property-scoped owner-statement backfills propagate to Cash Flow workbooks. |
| No-DAO mortgage liability | `scripts/baselane_reconcile_no_dao_mortgage_liability.py` | `config/no_dao_mortgage_liability_reconciliation.json`, exact Baselane transaction reads, statement/amortization evidence | Read-only, exact-ID liability waterfall; unresolved transfers and native-split differences stay in review. |
| Alawa ECO transfer allocation | `scripts/baselane_split_alawa_eco_transfers.py` | Five approved exact transfer IDs and statement-backed month allocation | Manual-only preview/digest/apply/readback; native children remain Transfers Between Accounts. |
| Yhome/Web3 reconciliation | `scripts/refresh_yhome_transition_reconciliation.py` | `yhome_*`, `baselane_web3_reconciliation_apply.py`, `baselane_fix_yhome_*`, `reconcile_*` | Read/compare first; reports ECO spendable custody and verified DAO A/P in separate columns; non-cash entries remain non-cash absent bank evidence. |
| Local CPAI shadow observer | `scripts/cpai_shadow_after_daily.sh` | `cpai_local_supervisor.py`, `config/cpai_supervisor_policy.json`, deterministic JSON reports | Local-only report triage; strict JSON, no command dispatch, no financial authority, and no external side effects. |
| Baselane MCP | `skills/baselane-mcp/src/baselane_mcp/server.py` | `transfers.py`, visible-CDP transport, Fold 7 MFA helper, durable transfer state | Guarded local tool API; exact-token internal transfers resume across OTP without duplicating cash. |

## Retained manual-only scripts

Files named for a specific property, date, payment, or remediation are preserved as an audit/replay record. They are not scheduled, do not create a standing policy, and must be run only under the live-action protocol in `AGENTS.md`. Before reuse, confirm the transaction IDs, property scope, accounting treatment, and target state still match live data.

## Configuration boundary

The checked-in `config/*.json` files encode mapping/classification and approved accounting decisions. They are part of production behavior. Runtime paths, auth state, browser/CDP endpoints, and secret material are local-only and intentionally absent.

## Dependency boundary

- [lofty-pm](https://github.com/earlvanze/lofty-pm) is the external companion repository for Lofty property maps, update payloads, and guarded publication.
- [Baselane MCP](../skills/baselane-mcp/) is vendored in this repository and installed from `skills/baselane-mcp`; it is not a separate Git clone.
- [Cashflow propagation](../skills/baselane-financials/SKILL.md) is vendored in this repository. It is the canonical Baselane/ECO-GL-to-property-workbook leg that feeds downstream Lofty live-financial review; it is used by weekly/monthly statement flows and by source-cash and Cashflow review tools.
- [requirements.txt](../requirements.txt) is the root Python bootstrap manifest. The MCP's `pyproject.toml` and `uv.lock` remain its package-specific resolved dependency source; do not hand-copy its transitive dependencies into another lockfile.

## Session boundary

Legacy cookie seeding and browser-profile persistence are intentionally not included. Every live lane uses the operator's authorized visible Brave session. Maintained login recovery may supply credentials at runtime from the approved secret store and reports the exact visible CDP `target_id` for tab pinning. The bank-SMS recovery lane starts with the already-previewed transfer's own OTP-required response, followed automatically by `scripts/baselane_fold7_mfa.py` and `scripts/baselane_verify_bank_otp.js` when `auto_mfa=true`. `scripts/baselane_request_bank_otp.js` is an optional one-code resend helper only when the authenticated user can access Baselane's standalone bank-verification resolver; its rejection does not imply that a collaborator lacks transfer authority. The Fold helper discovers exactly one already-authorized Tailnet Fold target or requires an explicit Tailnet target, selects a fresh unused Baselane SMS, keeps the OTP in process memory and the child environment only, and writes the returned short-lived token only into the same visible Baselane tab.

## Local-model boundary

The local model is not a scheduler, source of truth, or action engine. `cpai_local_supervisor.py` accepts only bounded local report inputs, binds every response to their SHA-256 manifest, and emits an advisory `DecisionEnvelope` in shadow mode. It has no dispatch implementation by design. See [Local CPAI](LOCAL_CPAI.md) and the [workflow diagram](WORKFLOW.md).
