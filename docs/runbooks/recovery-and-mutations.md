# Recovery and mutation controls

Use a recovery workflow only to resume an idempotent job after its evidence shows the exact pending scope. Never replay a run because an earlier browser/auth attempt failed; restore the authorized visible session, rerun the read-only preflight, and use the existing recovery script.

For every mutation:

1. Preserve the dry-run JSON/report and exact confirmation digest.
2. Apply one bounded workflow.
3. Refresh from the upstream system independently.
4. Verify categories, property tags, counterpart/mirror rows, native components, and whether cash actually moved.
5. Record the result in the appropriate ignored local report and commit only code or policy changes.

If the action concerns a transfer, it must stay inside the Baselane workspace and preserve category 24 on both mirrored bank entries. A Baselane bank-SMS challenge sent to the authorized Fold 7 may be handled only by the canonical Tailnet-only `scripts/baselane_fold7_mfa.py` workflow. If the action would need an external recipient, a new cash instruction, unsupported authentication challenge handling, or a guess about an accrual, stop and obtain a human decision.

## Internal cash transfer recovery

Use `transfer_cash` as the sole state machine: dry-run, review, then apply the
same inputs with its exact confirmation token. The apply path requests any
required OTP through the transfer mutation itself, maps a child source account
to its parent `mfa_bank_account_id`, runs the Fold 7 helper, and retries only
that same idempotent request. It never uses `send_money` or an external payee.

- `authentication_required` or `mfa_pending` means Baselane rejected the
  mutation before cash moved. Retrying the exact token after MFA is safe.
- `completed` or `already_completed` means the idempotency state recorded one
  verified receipt. Refresh and match both mirrored entries.
- `reconciliation_required` means the submission outcome may be uncertain.
  Never retry it. Run `get_transfer_status`, inspect Baselane upstream, and
  reconcile the existing request first.

The optional standalone OTP resend helper can be unavailable to a collaborator
who remains authorized to transfer. Its `UNAUTHORIZED_ACCESS` response applies
only to resend and must never be interpreted as lost transfer authority.

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
