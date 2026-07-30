# Baselane Manual Categorization Instructions
## Wyoming SOS Transactions (66 transactions)

### Overview
- **Total transactions**: 66 Wyoming SOS entries
- **Categorization**: Complete in CSV (56.3% overall coverage)
- **Upstream status**: Blocked (CDP auth requires manual login)
- **Alternative**: Manual categorization in Baselane UI

### Step 1: Identify Wyoming SOS Transactions

1. Log into Baselane: https://app.baselane.com
2. Navigate to: Banking → Transactions
3. Filter by: Search "WYOMING" or "I3B*WY SECRETARY"
4. Select date range: All time (to capture all 66 transactions)

### Step 2: Categorize by Amount Pattern

For each Wyoming SOS transaction, set:
- **Type**: Operating Expenses
- **Category**: Tax Licenses & Registrations
- **Sub-category**: Based on amount:

| Amount | Sub-category | Description |
|--------|-------------|-------------|
| -62.25 | WY SOS Annual Report | Annual report filing |
| -61.44 | WY SOS Filing Fee | Standard filing fee |
| -103.75 | WY SOS Combined Filing | Combined filing fee |
| -102.40 | WY SOS Amendment | Amendment filing |
| Other | WY SOS Other | Miscellaneous SOS fees |

### Step 3: Assign Properties

The transactions should be assigned to physical property addresses (not LLC names):
- 20 Tennessee Ave
- 27 S Beard St
- 27 Pillar Ln
- 49 Bannbury Ln
- 84 Madison
- 86 Madison
- 88 Madison
- 90 Madison
- And others (see full list in baselane_wyoming_split_plan.json)

### Step 4: Upload Receipts (Optional but Recommended)

Receipts are located in:
- `/mnt/c/Users/digit/Dropbox/Real Estate/FL/27 Pillar Ln, Palm Coast, FL 32164/Public/03 - LLC Documents/`
- `/mnt/c/Users/digit/Dropbox/Real Estate/FL/49 Bannbury Ln, Palm Coast, FL 32137/Public/03 - LLC Documents/`
- `/mnt/c/Users/digit/Dropbox/Entities/LFTY0412 LLC/`
- `/mnt/c/Users/digit/Dropbox/Entities/LFTY400 LLC/`

Upload matching receipts to each transaction in Baselane.

### Step 5: Verify

After categorization:
1. Filter by Category: "Tax Licenses & Registrations"
2. Verify all 66 Wyoming SOS transactions appear
3. Check sub-categories match the amount patterns above
4. Confirm properties are assigned correctly

### Next Vendors to Process (Automated)

Once Wyoming is complete, proceed with:

1. **OSC Risk Secure** (45 properties)
   - Category: Insurance
   - Sub-category: OSC Risk Secure

2. **ECO Systems Internal Transfers** (45 properties)
   - Category: Transfers Between Accounts
   - Sub-category: ECO Systems Internal

3. **Aligned Properties** (32 properties)
   - Category: Property Management
   - Sub-category: Aligned Properties

### CSV Export for Assetrail

Current export available at:
- `Dropbox/Projects/Baselane/exports/baselane_transactions_20260719.csv`
- Total: 9,374 transactions
- Categorized: 5,277 (56.3%) + 186 additional = ~58%

### Supporting Files

- Split plan: `reports/baselane_wyoming_split_plan.json`
- Categorized CSV: `reports/baselane_source_transaction_index_categorized.csv`
- Receipt index: `reports/baselane_additional_categorization.json`

### CDP Automation (Future)

To re-enable automatic upstream categorization:
1. Ensure Brave CDP is running on port 9222
2. Log into Baselane in the CDP browser
3. Re-run: `python3 scripts/baselane_apply_wyoming_splits.py`

---
Generated: 2026-07-19
Ready for manual execution
