# Daily finance sync

1. Confirm the source ledger location, available disk space, and an already authenticated visible Baselane browser session.
2. Set `OPENCLAW_WORKSPACE_ROOT` to this repository and set local-only `DROPBOX_ROOT` / `BASELANE_LEDGER_DIR` values.
3. Run `bash scripts/baselane_cron_run.sh` without live-action overrides.
4. Inspect the generated scope, sync, source-cash, stale-artifact, and run reports in the local ignored `reports/` directory.
5. If a source correction is proposed, stop at the plan/evidence stage unless the requested scope expressly authorizes a preview-digest apply.
6. Confirm canonical ledger row counts and split outputs before publishing or using a balance downstream.

Expected result: a refreshed, guarded local ledger and review artifacts. It is not permission to send money, amend Lofty, or alter a Google Sheet.
