# Recovery and mutation controls

Use a recovery workflow only to resume an idempotent job after its evidence shows the exact pending scope. Never replay a run because an earlier browser/auth attempt failed; restore the authorized visible session, rerun the read-only preflight, and use the existing recovery script.

For every mutation:

1. Preserve the dry-run JSON/report and exact confirmation digest.
2. Apply one bounded workflow.
3. Refresh from the upstream system independently.
4. Verify categories, property tags, counterpart/mirror rows, native components, and whether cash actually moved.
5. Record the result in the appropriate ignored local report and commit only code or policy changes.

If the action concerns a transfer, it must stay inside the Baselane workspace and preserve category 24 on both mirrored bank entries. If the action would need an external recipient, a new cash instruction, authentication challenge handling, or a guess about an accrual, stop and obtain a human decision.

## GraphQL execution

Scheduled Baselane reads and writes use direct GraphQL. The visible browser is
only an authorized source of short-lived session and App Check headers. The
transport reuses fresh cached headers, refreshes them through raw CDP when
needed, and does not use Playwright unless
`BASELANE_GQL_PLAYWRIGHT_FALLBACK=1` is explicitly set for manual recovery.

Full-ledger workflows batch bounded metadata and pagination operations through
one bridge process. A guarded write must still re-read its exact transaction
IDs before mutation and verify those same IDs afterward; a second full-ledger
scan is not a substitute for that check.
