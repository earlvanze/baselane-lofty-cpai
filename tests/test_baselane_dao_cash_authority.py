import unittest

from scripts.baselane_dao_cash_authority import apply_to_summary


class ApplyToSummaryTests(unittest.TestCase):
    def test_unmapped_property_does_not_treat_gl_as_cash_custody(self):
        authority = {
            "status": "ok",
            "path": "/reports/live-dao-cash.json",
            "as_of": "2026-07-31",
            "properties": {},
        }
        summary = {
            "eco_gl_column_e_status": "ok",
            "eco_gl_column_e_sum": 19_133.68,
            "eco_cash_basis_amount_as_of_month": 19_133.68,
            "eco_gl_column_e_source": "/ledgers/1456.csv",
            "eco_gl_column_e_as_of_date": "2026-07-31",
            "open_accrued_obligations": 837.47,
        }

        result = apply_to_summary(summary, "1456 W 85th St", authority)

        self.assertEqual(result["physical_bank_cash_status"], "property_missing")
        self.assertEqual(result["eco_held_cash_gross"], 0.0)
        self.assertEqual(result["eco_held_unrestricted_cash"], 0.0)
        self.assertEqual(result["total_dao_spendable_cash"], 0.0)
        self.assertEqual(result["eco_held_unrestricted_cash_status"], "ok")
        self.assertEqual(
            result["eco_operating_cash_source_mode"],
            "id_bearing_eco_account_intercompany_subledger",
        )

    def test_dedicated_account_does_not_fallback_to_property_gl(self):
        authority = {
            "status": "ok",
            "path": "/reports/live-dao-cash.json",
            "as_of": "2026-07-31",
            "generated_at": "2026-07-31T20:00:00+00:00",
            "properties": {
                "88 madison ave": {
                    "property": "88 Madison Ave",
                    "dao_bank_total": 1_209.88,
                    "accounts": [{}],
                }
            },
        }
        summary = {
            "eco_gl_column_e_status": "ok",
            "eco_gl_column_e_sum": 4_423.12,
            "eco_cash_basis_amount_as_of_month": 4_423.12,
            "open_accrued_obligations": 18_831.06,
        }

        result = apply_to_summary(summary, "88 Madison Ave", authority)

        self.assertEqual(result["physical_bank_cash_status"], "ok")
        self.assertIsNone(result["eco_held_unrestricted_cash"])
        self.assertEqual(result["eco_held_unrestricted_cash_status"], "reconciliation_pending")
        self.assertIsNone(result["total_dao_spendable_cash"])

    def test_verified_intercompany_payable_is_separate_from_spendable_cash(self):
        authority = {
            "status": "ok",
            "path": "/reports/live-dao-cash.json",
            "as_of": "2026-07-31",
            "properties": {},
            "intercompany": {
                "86 madison ave": {
                    "property": "86 Madison Ave",
                    "status": "ok",
                    "source_mode": "id_bearing_eco_account_intercompany_subledger",
                    "eco_held_dao_cash_before_obligations": "0.00",
                    "dao_accounts_payable_to_eco": "550.00",
                    "eco_accounts_receivable_from_dao": "550.00",
                    "gross_eco_advances": "1550.00",
                    "gross_dao_cash_credits": "1000.00",
                    "monthly_breakdown": [{"month": "2026-07"}],
                    "category_breakdown": [{"category": "Repairs"}],
                }
            },
        }
        summary = {
            "eco_gl_column_e_status": "ok",
            "eco_gl_column_e_sum": -550.00,
            "eco_cash_basis_amount_as_of_month": -550.00,
            "eco_gl_column_e_source": "/ledgers/86.csv",
            "eco_gl_column_e_as_of_date": "2026-07-31",
            "open_accrued_obligations": 0,
        }

        result = apply_to_summary(summary, "86 Madison Ave", authority)

        self.assertEqual(result["dao_accounts_payable_to_eco"], 550.00)
        self.assertEqual(result["eco_accounts_receivable_from_dao"], 550.00)
        self.assertEqual(result["intercompany_payable_status"], "ok")
        self.assertEqual(result["eco_held_unrestricted_cash"], 0.00)
        self.assertEqual(result["total_dao_spendable_cash"], 0.00)


if __name__ == "__main__":
    unittest.main()
