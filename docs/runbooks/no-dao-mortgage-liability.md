# No-DAO mortgage liability reconciliation

Use this workflow when a mortgage payment leaves a DAO-controlled bank account but the mortgage principal, interest, and lender late/return fees are contractually ECO's responsibility. It is read-only and does not split transactions, relabel transfers, publish summaries, or move cash.

## Run

1. Record each statement-supported payment and its exact Baselane parent ID in `config/no_dao_mortgage_liability_reconciliation.json`.
2. Decompose the payment into principal, interest, tax escrow, insurance escrow, general escrow, and lender fees. The configured components must equal the parent cash debit.
3. Record only purpose-supported, exact-ID ECO reimbursements as confirmed. Put blank, composite, or otherwise ambiguous inbound transfers in `candidate_reimbursements`.
4. Run:

```bash
python3 scripts/baselane_reconcile_no_dao_mortgage_liability.py \
  --property "85-104 Alawa Pl"
```

The monthly statements/P&I lane runs `--all-configured`, so adding another property to the policy registry automatically adds its read-only gate to the close:

```bash
python3 scripts/baselane_reconcile_no_dao_mortgage_liability.py --all-configured
```

For deterministic tests without Baselane access, supply an ID-keyed fixture with `--offline-transactions FILE`. The default report is `reports/no_dao_mortgage_liability_reconciliation.json` and is intentionally ignored by Git.

## Accounting waterfall

- `gross_pi_due_from_eco`: DAO-bank principal and interest that ECO must reimburse.
- `gross_lender_fees_due_from_eco`: lender late, NSF, return, or similar fees that ECO must reimburse.
- `restricted_dao_escrow_paid`: DAO-owned tax/insurance/other escrow. It is not spendable cash and is not ECO responsibility.
- `confirmed_eco_reimbursements`: only exact-ID, purpose-supported cash credits applied to ECO responsibility.
- `open_mortgage_due_from_eco`: P&I plus lender fees, less confirmed reimbursements.
- `other_dao_ap_to_eco`: separately supported PM fees, DAO fees, or other DAO obligations.
- `net_after_explicit_cross_entity_ap`: the net settlement direction after the two explicit entity liabilities are shown gross.

Never report the net alone. The gross due-from-ECO and DAO-payable components must remain visible so a reader can understand the direction and purpose of each obligation.

## Status and follow-up

- `ok`: every source identity matches, native components match, and no transfer is awaiting allocation.
- `review`: source identities match, but native split differences or candidate reimbursements remain. Do not silently apply candidates to the balance.
- `blocked`: an expected cash row is missing, inactive, or differs in bank account, date, amount, or reimbursement memo. Do not publish or move cash.

Native split differences are a repair queue, not authority to write. Build a separate exact-ID preview/apply/verify mutation plan under `AGENTS.md` before changing Baselane. After any approved correction, rerun this workflow from live state and use the new source digest.

For the approved 85-104 Alawa ECO-transfer allocation, the deterministic mutation lane is:

```bash
python3 scripts/baselane_split_alawa_eco_transfers.py
python3 scripts/baselane_split_alawa_eco_transfers.py \
  --apply --require-plan-digest "<digest from the live preview>"
python3 scripts/baselane_reconcile_no_dao_mortgage_liability.py \
  --property "85-104 Alawa Pl"
```

The split lane fails closed on parent identity or target-sum drift. All native transfer children use category 24; month and purpose are encoded in their labels so Cash Flow statements remain uncorrupted.

Investor-facing summaries must include: `If anything looks wrong, please DM @earlvanze on Discord or email ecosystemspm@gmail.com.`
