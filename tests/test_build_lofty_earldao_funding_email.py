import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_lofty_earldao_funding_email_20260729 as funding


class LoftyEarldaoFundingEmailTests(unittest.TestCase):
    def test_coownership_bank_gap_credits_lofty_or_toward_reserve_floor(self) -> None:
        coownership = (
            "Example Coownership",
            "Example Property",
            "Example Property",
            Decimal("0.00"),
        )
        active_rows = [
            {
                "property": "Example Property",
                "combined_eco_and_lofty_reserve": "3500.00",
                "lofty_curr_maintenance_reserve": "1000.00",
            }
        ]
        live_rows = [
            {
                "property": "Example Property",
                "operations_balance": "2500.00",
            }
        ]

        with patch.object(funding, "COOWNERSHIPS", (coownership,)):
            rows = funding.build_coownership_rows(active_rows, live_rows)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bank_and_lofty_liquidity"], Decimal("3500.00"))
        self.assertEqual(rows[0]["accounting_gap"], Decimal("0.00"))
        self.assertEqual(rows[0]["bank_gap"], Decimal("0.00"))
        self.assertEqual(rows[0]["request"], Decimal("0.00"))


if __name__ == "__main__":
    unittest.main()
