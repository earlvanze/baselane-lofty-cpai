# Monthly close and accruals

## 28th accrual lane

`bash scripts/baselane_monthly_accruals_28th_cron.sh` is the dedicated owner for current-month PM-fee and DAO-LLC-fee accrual writes. It runs only after the source ledger, eligibility rules, duplicate checks, and confirmation gates pass. The recovery checker records missed-run evidence but does not autonomously replay a live accrual.

## Month close

`bash scripts/baselane_financials_monthly_cron.sh` assembles the finance-truth refresh, statements, reconciliations, property review packets, and Lofty approval artifacts. In its normal mode, external publication, messages, and writes are off. Enable an action only after a human reviews the generated packet and authorizes the exact destination.

Complete a close only when the source ledger, accrual completeness, statement checks, mortgage integrity, cash-flow controls, and publication guards have all reached their documented state. A review/blocked state is a valid result and must remain visible.

## Cash and intercompany summary controls

Run `scripts/baselane_live_dao_cash_reconciliation.py` from the current ID-bearing source ledger before building review candidates. The output is the authority for both custody and verified intercompany debt:

1. Gross ECO-held DAO cash is reduced only by recorded unpaid obligations to produce nonnegative spendable cash.
2. A negative transaction-backed ECO intercompany position is reported separately as `DAO A/P - Due to ECO for Verified Advances`, with an equal `ECO A/R - Due from DAO`.
3. A diagnostic shortfall, a property tag, or a GL Column E total cannot create custody cash or A/P by itself.
4. The monthly owner summary and Yhome reconciliation must use the same labels. Omit a dedicated DAO-bank line when no dedicated account exists, and never expose Column E as a cash summary.

The monthly review candidate packet is a local review artifact. Publishing to Lofty, Discord, or email remains a separately authorized action after the generated values and wording are reviewed.

## Investor review SOP

After the all-property send plan and its financial validation are clean, the monthly cron delegates review-draft delivery to OpenClaw `agent:discord-public`. It posts two messages in each property's routed EARLCoin Guild `ecosystems-pm` forum thread: a short review header followed by the exact standalone property draft marked `DRAFT FOR REVIEW - NOT EMAILED`.

The sender is digest-verified and idempotent. A non-`ok` plan is rejected locally before the agent starts, and successful message receipts are persisted after every property so a retry does not duplicate prior posts. This review stage does not mutate Lofty and does not send owner email.

Atlas Intel may collect edits or a DAO-scoped approval from the corresponding forum thread. Owner email remains a later, separately enabled final stage and must stay disabled until the applicable DAO approval and the existing financial, recipient, cooldown, and send-evidence guards all pass. One DAO's approval must not imply approval for another DAO.

## National Grid and ESCO anomaly lane

The monthly close first runs the `national-grid-statements` coverage audit and then runs `scripts/national_grid_bill_analyzer.py` before Lofty draft preparation. Its configured scope is January 2025 onward for 84, 86, 88, and 90 Madison Ave plus 724 3rd Ave. It reads National Grid PDFs and available National Grid/Arcadia CSV history, caches PDF extraction, and writes:

- `reports/national_grid_statement_audit.json`
- `reports/national_grid_bill_analyzer.json`
- `reports/national_grid_bill_analyzer.md`

Missing statements within the January 2025-onward window remain a visible coverage-review state; older gaps are out of scope. The analyzer compares each account month over month for current-charge increases, supplier-rate increases, stale evidence, and the critical combination of a stable payment, rising underlying consumption charges, and a growing deferred balance. American Power & Gas statements are labeled as the disputed variable-rate pattern for review. A critical anomaly remains a visible review condition and does not itself perform a payment, ESCO change, owner send, or other external action.

The JSON report is passed to the Lofty monthly update driver. Matching property drafts, the portfolio summary, the index, and the monthly review checklist receive a `UTILITY_ANOMALY` alert. Existing month-stamped drafts are preserved; their anomaly remains visible in the index, summary, and checklist until an operator reviews it.

The analyzer's `apg_portfolio_review` adds a masked-account, statement-level timeline for every configured account whose supplier is American Power & Gas. It records the first observable fixed-payment/rising-charge/deferred-balance divergence and the latest statement snapshot. This supports portfolio-wide factual review without disclosing account numbers or asserting a legal conclusion.
