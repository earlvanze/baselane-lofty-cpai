import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "yhome_operating_cash_gsheet_update.py"
SPEC = importlib.util.spec_from_file_location("yhome_operating_cash_gsheet_update", SCRIPT)
assert SPEC and SPEC.loader
updater = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updater)


class LiveRowValidationTests(unittest.TestCase):
    def test_equivalent_address_formatting_does_not_drift(self):
        expected = [
            {
                "property": "1432 Sara Ave, Akron, Ohio 44305",
                "current_value": -515.96,
                "sheet_title": "Cleveland",
                "row_number": 20,
                "target_cell": "W20",
            },
            {
                "property": "566 Nash St, Akron, OH 44306",
                "current_value": 100.0,
                "sheet_title": "Cleveland",
                "row_number": 19,
                "target_cell": "W19",
            },
        ]
        payload = {
            "valueRanges": [
                {"values": [["1432 Sara Ave. Akron, OH 44305"]]},
                {"values": [[-515.96]]},
                {"values": [["566 Nash Street, Akron, OH 44306"]]},
                {"values": [[100.0]]},
            ]
        }
        self.assertEqual(updater.validate_live_row_payload(expected, payload), [])

    def test_different_street_address_still_fails_closed(self):
        expected = [
            {
                "property": "1432 Sara Ave, Akron, Ohio 44305",
                "current_value": -515.96,
                "sheet_title": "Cleveland",
                "row_number": 20,
                "target_cell": "W20",
            }
        ]
        payload = {
            "valueRanges": [
                {"values": [["1456 W 85th St. Cleveland, OH 44102"]]},
                {"values": [[-515.96]]},
            ]
        }
        issues = updater.validate_live_row_payload(expected, payload)
        self.assertEqual(issues[0]["type"], "live_row_property_mismatch")


if __name__ == "__main__":
    unittest.main()
