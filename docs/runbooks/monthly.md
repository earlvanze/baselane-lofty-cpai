# Monthly close and accruals

## 28th accrual lane

`bash scripts/baselane_monthly_accruals_28th_cron.sh` is the dedicated owner for current-month PM-fee and DAO-LLC-fee accrual writes. It runs only after the source ledger, eligibility rules, duplicate checks, and confirmation gates pass. The recovery checker records missed-run evidence but does not autonomously replay a live accrual.

## Month close

`bash scripts/baselane_financials_monthly_cron.sh` assembles the finance-truth refresh, statements, reconciliations, property review packets, and Lofty approval artifacts. In its normal mode, external publication, messages, and writes are off. Enable an action only after a human reviews the generated packet and authorizes the exact destination.

Complete a close only when the source ledger, accrual completeness, statement checks, mortgage integrity, cash-flow controls, and publication guards have all reached their documented state. A review/blocked state is a valid result and must remain visible.
