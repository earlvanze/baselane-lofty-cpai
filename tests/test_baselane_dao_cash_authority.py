import unittest

from scripts.baselane_dao_cash_authority import apply_to_summary


class ApplyToSummaryTests(unittest.TestCase):
    def test_active_property_without_dedicated_account_omits_bank_but_keeps_ap(self):
        authority = {
            "status": "ok",
            "path": "/reports/live-dao-cash.json",
            "as_of": "2026-07-31",
            "properties": {
                "1456 w 85th st": {
                    "property": "1456 W 85th St",
                    "dao_bank_total": 0,
                    "eco_held_cash_gross": 0,
                    "eco_held_unrestricted_cash": 0,
                    "open_accrued_obligations": 750,
                    "dao_accounts_payable_by_counterparty": [
                        {
                            "counterparty": "ECO Systems LLC",
                            "relationship": "management_company",
                            "category": "Cash advances",
                            "amount": "20959.51",
                            "cash_effect": "payable",
                        }
                    ],
                    "dao_accounts_receivable_by_counterparty": [],
                    "total_dao_spendable_cash": 0,
                    "accounts": [],
                }
            },
        }

        result = apply_to_summary({}, "1456 W 85th St", authority)

        self.assertEqual(result["physical_bank_cash_status"], "property_missing")
        self.assertIsNone(result["physical_bank_cash"])
        self.assertEqual(result["counterparty_balances_status"], "ok")
        self.assertEqual(
            result["dao_accounts_payable_by_counterparty"],
            [
                {
                    "counterparty": "ECO Systems LLC",
                    "relationship": "management_company",
                    "category": "Cash advances",
                    "amount": 20959.51,
                    "cash_effect": "payable",
                }
            ],
        )

    def test_ohio_3_package_uses_1518_dille_dao_bank_account(self):
        authority = {
            "status": "ok",
            "path": "/reports/live-dao-cash.json",
            "as_of": "2026-07-31",
            "generated_at": "2026-08-02T18:53:10+00:00",
            "properties": {
                "1518 dille rd": {
                    "property": "1518 Dille Rd",
                    "dao_bank_total": 9.80,
                    "dao_bank_spendable_before_obligations": 9.80,
                    "eco_held_cash_gross": 0,
                    "eco_held_unrestricted_cash": 0,
                    "open_accrued_obligations": 62.50,
                    "open_accrued_obligations_by_kind": {"dao": "62.50"},
                    "total_dao_spendable_cash": 0,
                    "accounts": [{"account_name": "Lofty Holding 1518 Dille Road DAO LLC"}],
                }
            },
        }

        result = apply_to_summary({}, "Ohio 3-Property Package", authority)

        self.assertEqual(result["physical_bank_cash_status"], "ok")
        self.assertEqual(result["physical_bank_cash"], 9.80)
        self.assertEqual(result["physical_bank_cash_matched_property"], "1518 Dille Rd")
        self.assertEqual(result["open_accrued_obligations"], 62.50)

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
            "verified_eco_cash_custody_reconciliation",
        )
        self.assertEqual(result["eco_operating_cash"], 0.0)

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

    def test_dedicated_dao_bank_cash_is_not_eco_net_dao_funds(self):
        authority = {
            "status": "ok",
            "path": "/reports/live-dao-cash.json",
            "as_of": "2026-07-31",
            "generated_at": "2026-07-31T20:00:00+00:00",
            "properties": {
                "90 madison ave": {
                    "property": "90 Madison Ave",
                    "dao_bank_total": 2_000,
                    "dao_bank_spendable_before_obligations": 2_000,
                    "eco_held_cash_gross": 100,
                    "eco_held_unrestricted_cash": 100,
                    "open_accrued_obligations": 150,
                    "open_accrued_obligations_by_kind": {"pm": "100.00", "legal": "50.00"},
                    "total_dao_spendable_cash": 1_950,
                    "accounts": [{}],
                }
            },
        }

        result = apply_to_summary({}, "90 Madison Ave", authority)

        self.assertEqual(result["physical_bank_cash"], 2_000)
        self.assertEqual(result["total_dao_spendable_cash"], 1_950)
        self.assertEqual(result["eco_operating_cash"], 100)
        self.assertEqual(result["open_accrued_obligations_by_kind"], {"pm": 100.0, "legal": 50.0})
        self.assertEqual(result["eco_operating_cash_balance_scope"], "eco_held_unrestricted_cash_only")

    def test_unreconciled_intercompany_activity_does_not_create_a_payable(self):
        authority = {
            "status": "ok",
            "path": "/reports/live-dao-cash.json",
            "as_of": "2026-07-31",
            "properties": {},
            "intercompany": {
                "86 madison ave": {
                    "property": "86 Madison Ave",
                    "status": "reconciliation_pending",
                    "source_mode": "id_bearing_eco_account_activity_trace",
                    "eco_held_dao_cash_before_obligations": "0.00",
                    "dao_accounts_payable_to_eco": "0.00",
                    "eco_accounts_receivable_from_dao": "0.00",
                    "candidate_dao_accounts_payable_to_eco": "550.00",
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

        self.assertEqual(result["dao_accounts_payable_to_eco"], 0.00)
        self.assertEqual(result["eco_accounts_receivable_from_dao"], 0.00)
        self.assertEqual(result["intercompany_payable_status"], "reconciliation_pending")
        self.assertEqual(result["eco_held_unrestricted_cash"], 0.00)
        self.assertEqual(result["total_dao_spendable_cash"], 0.00)


if __name__ == "__main__":
    unittest.main()
