# Monthly close and accruals

## 28th accrual lane

`bash scripts/baselane_monthly_accruals_28th_cron.sh` is the dedicated owner for current-month PM-fee and DAO-LLC-fee accrual writes. It runs only after the source ledger, eligibility rules, duplicate checks, and confirmation gates pass. The recovery checker records missed-run evidence but does not autonomously replay a live accrual.

For every active Lofty property DAO, the DAO registration/admin charge is
$750.00 per annual service cycle, recognized as a paired $62.50 monthly
accrual: a DAO legal/admin expense and matching ECO `Fees & Other Revenue`.
ECO's $200.00 annual amount payable to Lofty and ECO's actual filing/vendor
costs (historical reference $125.00) are separate ECO expenses. They must not
offset, reduce, or replace the DAO's $750.00 payable. A cash transfer settles
the accrued DAO payable; it does not create a second fee or reverse the P&L
pair. Sold, inactive, archived, and entity-only rows are excluded.

## Month close

`bash scripts/baselane_financials_monthly_cron.sh` assembles the finance-truth refresh, statements, reconciliations, property review packets, and Lofty approval artifacts. In its normal mode, external publication, messages, and writes are off. Enable an action only after a human reviews the generated packet and authorizes the exact destination.

OpenClaw may rebuild the review chain through
`baselane.rebuild_monthly_review`. That MCP operation validates the month and
cutoff, invokes this exact entry point, and forcibly disables accrual applies,
interest/reserve cash movement, Google Sheet writes, Lofty updates, Discord or
Telegram sends, and owner email. Inspect its output with
`baselane.get_pipeline_artifact`; that reader accepts only the named canonical
reports and never an arbitrary filesystem path.

Complete a close only when the source ledger, accrual completeness, statement checks, mortgage integrity, cash-flow controls, and publication guards have all reached their documented state. A review/blocked state is a valid result and must remain visible.

After the live DAO cash report is clean, the close runs the idempotent DAO
savings-interest sweep planner. It transfers only validated bank-interest cash
to ECO 2624; documented security deposits, reserve principal, operating floors,
loan interest, and inferred balance differences are ineligible. Dry runs write
`reports/baselane_monthly_interest_sweep_plan.json`. A live-approved close binds
apply to that digest, records durable transfer state, handles one Fold bank-SMS
challenge, and regenerates the live DAO cash report after success.
The recurring internal sweep has standing authorization independent of email,
Discord, and Lofty publication flags. Set `DRY_RUN=1` for an explicit
no-mutation rehearsal or `APPLY_MONTHLY_DAO_INTEREST_SWEEP_LIVE=0` to disable
only the sweep apply while retaining its plan.

## Cash and intercompany summary controls

For co-ownerships, the $3,000 minimum reserve is a combined liquidity floor:
positive Lofty Operating Reserve plus ECO-held spendable DAO cash. Do not demand
an additional $3,000 in ECO cash when Lofty already holds part or all of the
floor. Any ECO-to-Lofty transfer is limited to the combined surplus and capped
by non-negative cash actually held by ECO.

The live Baselane cash reconciliation does not impose a second co-ownership
bank-account floor. Its co-ownership local-bank float is zero; the guarded
transfer planner applies the $3,000 combined test after joining the current
Lofty OR balance. The separate $500 non-co-ownership operating-float policy is
unchanged.

Run `scripts/baselane_live_dao_cash_reconciliation.py` from the current ID-bearing source ledger before building review candidates. The output is the authority for both custody and verified intercompany debt:

The deterministic Baselane sync must refresh the canonical ledger and the
ID-bearing `baselane_source_transaction_index.csv` from the same authenticated
session. A missing file or SHA-256 mismatch blocks downstream accrual and split
work so a stale index cannot cause duplicate manual rows.

1. Gross ECO-held DAO cash is reduced only by recorded unpaid obligations to produce nonnegative spendable cash.
2. The negative side of the exact ID-bearing ECO-account cash rollforward (actual ECO advances less actual DAO cash credits/reimbursements) is reported separately as `DAO A/P - Due to ECO (unreimbursed cash advances)`. Its equal ECO receivable remains an internal double-entry control and is not duplicated in the DAO summary. Other accrued property obligations are itemized by counterparty and category.
3. A property tag, diagnostic shortfall, GL Column E total, or non-ID-bearing historical trace cannot create custody cash or A/P by itself. A positive historical cash rollforward likewise cannot establish current ECO custody without physical-cash evidence.
4. The monthly owner summary and Yhome reconciliation must use the same labels. Omit a dedicated DAO-bank line when no dedicated account exists, and never expose Column E as a cash summary.
5. Show Baselane/ECO custody cash after obligations separately from the signed Lofty Operating Reserve ledger. Do not publish their sum as a second “reserve-adjusted operating position”; the combined calculation is an internal reserve-floor test only.
6. Bind every monthly summary to one exact reporting cutoff. For the July 2026 close that date is `2026-07-31`. The GL cutoff and dated cash authority must both equal the packet cutoff; a July 14 (or any other same-month) source is stale and blocks publication.

Blank or generic ECO-side property tags are never inferred broadly. Record an evidenced historical correction in `config/baselane_intercompany_transaction_overrides.json` with its exact source fields and reciprocal Baselane ID or specific external-ledger evidence. The reconciliation validates the complete rule before using it, verifies referenced Baselane evidence exists in the same ID-bearing source index, and embeds the policy SHA-256 digest in the output. If a rule or its evidence drifts, the monthly close stops before candidate summaries are generated; do not bypass the failure or reuse the previous report.

The monthly review candidate packet is a local review artifact. Its Discord review destination is EARLCoin only. Publishing a financial summary to the Lofty Investors guild or sending owner email remains a separately authorized action after the generated values and wording are reviewed.

For a property whose mortgage is not a DAO obligation, run the [no-DAO mortgage liability workflow](no-dao-mortgage-liability.md) before producing its monthly cash waterfall. Its report separates DAO-paid principal/interest and lender fees due from ECO from restricted mortgage escrow, and refuses to treat an unlabeled or composite transfer as a reimbursement. A `blocked` report forbids publication or cash movement; a `review` report must disclose its unresolved candidates and split differences.

Closed Cash Flow Statement columns use the canonical property GL through the target month-end (`as_of_month_end`). Current-control jobs outside the close retain the complete `full_column_e` view, and the Yhome spreadsheet remains an explicitly current full-column view. Target-month AOPS synthetic rows remain in the close even when an earlier operational cutoff is supplied; transactions dated in a later month never enter the closed column.

## Investor review SOP

Summary coverage is portfolio-wide, not a legacy hand-selected subset. Build the close-month authority with `scripts/lofty_monthly_active_roster.py` from `config/lofty_active_property_roster_policy.json`, the current ownership schedule, and a fresh Lofty manager snapshot. For the July 2026 close, require exactly 32 active physical properties and 30 grouped reporting targets, then generate one current-month financial summary for every reporting target. Selling properties remain active. Evidenced sold, closed, delisted, inactive or archived properties, entity-only rows, and split-only exclusions do not count and cannot substitute for an active target. A packet with fewer than 30 summaries—including the former 20-property packet—must fail closed as incomplete and must not be treated as a completed monthly publication set.

After the all-property send plan and its financial validation are clean, the monthly cron delegates review-draft delivery to OpenClaw `agent:discord-public`. The only unattended summary destination is the EARLCoin guild (`1473153860376858756`), in the `eco-systems-pm` review forum (`1480241103528530141`). It sends one short review header followed by one logical standalone property draft marked `DRAFT FOR REVIEW - NOT EMAILED`. OpenClaw may transport-chunk that draft into adjacent Discord messages; those fragments must reconstruct the planned draft exactly once and are not separate draft publications.

The plan and sender both validate the destination class, guild ID, forum ID, parent target, and per-property thread name before OpenClaw is invoked. The sender is digest-verified and idempotent; successful message receipts are persisted after every property so a retry does not duplicate prior posts. The legacy `discord_property_update` cron stage only mirrors this review report for compatibility and cannot invoke a second sender or fall back to the Lofty guild.

For an incremental roster expansion or a corrected single-property draft, pass
one or more exact `--property` values to the review sender. The sender still
requires the complete portfolio plan and its validation to be clean, then
delivers only the selected records and records both the portfolio and selected
counts in its receipt. Do not construct a reduced plan to bypass the population
gate.

Scheduled monthly and recovery runs keep the separate transfer-reconciliation Telegram summary disabled. Automated financial summary review has one destination: the EARLCoin `eco-systems-pm` forum.

The Lofty Investors guild (`847877825373012018`) is a publication destination, not a review destination. A financial summary can be posted there only through the single-property publication command with a human approval file bound to the run month, property, Lofty target, exact message SHA-256, approver, and approval timestamp. A live all-property Lofty send is forbidden.

A missing or stale hash-bound Lofty financial approval blocks only the later
Lofty listing mutation. It does not block a source-clean, clearly labelled
EARLCoin operator-review draft; otherwise the review artifact needed to obtain
approval could never be delivered. Any source-freshness, cash-reconciliation,
or financial-content issue still blocks the entire review batch.

Atlas Intel may collect edits or a DAO-scoped approval from the corresponding EARLCoin forum thread. Lofty guild publication and owner email are later, separately enabled final stages and must stay disabled until the applicable property or DAO approval and the existing financial, recipient, cooldown, and send-evidence guards all pass. One DAO's approval must not imply approval for another DAO.

## National Grid and ESCO anomaly lane

The monthly close first runs the `national-grid-statements` coverage audit and then runs `scripts/national_grid_bill_analyzer.py` before Lofty draft preparation. Its configured scope is January 2025 onward for 84, 86, 88, and 90 Madison Ave plus 724 3rd Ave. It reads National Grid PDFs and available National Grid/Arcadia CSV history, caches PDF extraction, and writes:

- `reports/national_grid_statement_audit.json`
- `reports/national_grid_bill_analyzer.json`
- `reports/national_grid_bill_analyzer.md`

Missing statements within the January 2025-onward window remain a visible coverage-review state; older gaps are out of scope. The analyzer compares each account month over month for current-charge increases, supplier-rate increases, stale evidence, and the critical combination of a stable payment, rising underlying consumption charges, and a growing deferred balance. American Power & Gas statements are labeled as the disputed variable-rate pattern for review. A critical anomaly remains a visible review condition and does not itself perform a payment, ESCO change, owner send, or other external action.

The JSON report is passed to the Lofty monthly update driver. Matching property drafts, the portfolio summary, the index, and the monthly review checklist receive a `UTILITY_ANOMALY` alert. Existing month-stamped drafts are preserved; their anomaly remains visible in the index, summary, and checklist until an operator reviews it.

The analyzer's `apg_portfolio_review` adds a masked-account, statement-level timeline for every configured account whose supplier is American Power & Gas. It records the first observable fixed-payment/rising-charge/deferred-balance divergence and the latest statement snapshot. This supports portfolio-wide factual review without disclosing account numbers or asserting a legal conclusion.
