# Cashflow Propagation

`scripts/update_cf_statements.py` is the canonical workbook propagation component of this repository. It converts the reviewed Baselane/ECO Systems general ledger into the per-property Cash Flow Statement workbooks used by downstream `FINANCIALS.md` generation and Lofty live-financial review.

## Contract

- Input: a canonical Baselane/ECO GL CSV, optionally a raw source-cash GL CSV, plus a closed reporting month and property scope.
- Output: the corresponding property workbooks below `Real Estate/**/07 - P&L & Owner Statements/`, plus local audit and discovery JSON reports.
- Writes only ordinary value cells. Formula cells are preserved. Rows derived from balance-sheet logic remain under their dedicated guarded updaters.
- A mismatch above the configured threshold is reported as a conflict. A conflict is evidence for reconciliation, never authorization to change a Baselane transaction, an accrual, a transfer, or a Lofty listing.
- It never contacts Baselane, Lofty, a bank, Google Sheets, or messaging services. It can write only the explicitly scoped local Cash Flow workbooks and local reports.
- The weekly and monthly completion gates must treat a missing, failed, or review-state propagation report as a blocker for `FINANCIALS.md` and Lofty publication. Do not bypass this component with a second mapping implementation.

## Usage

Use the scheduled weekly/monthly lanes whenever possible. For a scoped local review:

```bash
python3 skills/baselane-financials/scripts/update_cf_statements.py \
  --gl-csv /approved/path/ECO\ Systems\ General\ Ledger.csv \
  --source-cash-gl-csv /approved/path/ECO\ Systems\ General\ Ledger.csv \
  --output-dir /approved/path/Real\ Estate \
  --month 2026-07 \
  --property "84 Madison Ave" \
  --audit
```

Remove `--audit` only for a closed period after reviewing the audit report and confirming the workbook/property scope. `--allow-incomplete-month` is an intentional exception for current-period actuals and should not be used by routine automation.

## Runtime dependency

The configured Python interpreter must provide `openpyxl`. Repository verification checks that dependency so a missing Excel runtime fails before the weekly/monthly pipeline reaches a partial downstream state.
