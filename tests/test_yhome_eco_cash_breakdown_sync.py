import importlib.util
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "yhome_eco_cash_breakdown_sync.py"
SPEC = importlib.util.spec_from_file_location("yhome_eco_cash_breakdown_sync", SCRIPT)
assert SPEC and SPEC.loader
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


class LiveCashOverlayTests(unittest.TestCase):
    def test_clean_live_cash_overrides_stale_audit_amounts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit = root / "audit.json"
            source = root / "source.csv"
            live = root / "live.json"
            audit.write_text(json.dumps({"properties": [{
                "property": "85-104 Alawa Pl",
                "source_property": "85-104 Alawa Pl",
                "sheet_title": "Cleveland",
                "sheet_row": 2,
                "eco_cash_before_yhome": -36000,
                "expected_negative_yhome_adjustment": -100,
            }]}))
            source.write_text("Property,Account,Amount,Notes\n", encoding="utf-8")
            live.write_text(json.dumps({"status": "ok", "issues": [], "properties": [{
                "property": "85-104 Alawa Pl",
                "eco_held_cash_gross": "-12975.01",
                "open_accrued_obligations": "1087.67",
                "eco_held_restricted_cash": "0.00",
                "eco_held_unrestricted_cash": "-14062.68",
            }], "intercompany_subledger": [{
                "property": "85-104 Alawa Pl",
                "status": "ok",
                "source_mode": "id_bearing_eco_account_intercompany_subledger",
                "dao_accounts_payable_to_eco": "14062.68",
                "eco_accounts_receivable_from_dao": "14062.68",
            }]}))
            with patch.object(sync, "load_policy") as load_policy:
                load_policy.return_value.outstanding_manual_accrual_liability.return_value = 0
                rows = sync.load_breakdown_rows(audit, source, date(2026, 7, 31), live)
            self.assertEqual(rows[0]["eco_held_dao_cash_gross"], 0.0)
            self.assertEqual(rows[0]["accrued_but_unpaid_obligations"], 1087.67)
            self.assertEqual(rows[0]["eco_net_dao_funds"], 0.0)
            self.assertEqual(rows[0]["dao_accounts_payable_to_eco"], 14062.68)

    def test_intercompany_only_row_does_not_overwrite_cash_breakdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit = root / "audit.json"
            source = root / "source.csv"
            live = root / "live.json"
            audit.write_text(json.dumps({"properties": [{
                "property": "Example",
                "source_property": "Example",
                "sheet_title": "Cleveland",
                "sheet_row": 2,
                "eco_cash_before_yhome": 1200,
                "expected_negative_yhome_adjustment": -100,
            }]}))
            source.write_text("Property,Account,Amount,Notes\n", encoding="utf-8")
            live.write_text(json.dumps({"status": "ok", "issues": [], "properties": [],
                "intercompany_subledger": [{
                    "property": "Example",
                    "status": "ok",
                    "dao_accounts_payable_to_eco": "450.00",
                }]}))
            with patch.object(sync, "load_policy") as load_policy:
                load_policy.return_value.outstanding_manual_accrual_liability.return_value = 0
                rows = sync.load_breakdown_rows(audit, source, date(2026, 7, 31), live)
            self.assertEqual(rows[0]["eco_held_dao_cash_gross"], 1200.0)
            self.assertEqual(rows[0]["eco_net_dao_funds"], 1100.0)
            self.assertEqual(rows[0]["dao_accounts_payable_to_eco"], 450.0)

    def test_unclean_live_report_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "live.json"
            path.write_text(json.dumps({"status": "review", "issues": ["bad"]}))
            with self.assertRaises(ValueError):
                sync.load_live_cash_rows(path)


if __name__ == "__main__":
    unittest.main()
