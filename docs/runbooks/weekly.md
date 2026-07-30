# Weekly file update pass

Run `bash scripts/baselane_weekly_file_updates_cron.sh` from the repository root. It defaults to review-only behavior: live actions, Google Sheet writes, native-split application, and future cash-flow writes remain disabled unless both the global and action-specific gates are explicitly enabled.

Review the unprocessed-transaction report, mortgage workflow gate, cash-flow review gate, source reconciliation evidence, and Lofty review packet. Resolve exceptions with the smallest deterministic workflow; do not use a weekly batch run to repair historic balances without transaction-level evidence.
