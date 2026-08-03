from pathlib import Path
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import baselane_statements_operator as operator


class BaselaneStatementsOperatorTests(unittest.TestCase):
    def test_property_account_routing_precedes_eco_entity_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            downloads = root / "downloads"
            downloads.mkdir()
            property_name = (
                "BASELANE_ECO SYSTEMS, LLC_88 MADISON AVE OPERATIONS_"
                "JUL_2026_STATEMENT.pdf"
            )
            generic_name = (
                "BASELANE_ECO SYSTEMS, LLC_ECO SYSTEMS OPERATIONS_"
                "JUL_2026_STATEMENT.pdf"
            )
            personal_name = (
                "BASELANE_EARL VANZE CO_CAPITAL ACCOUNT_"
                "JUL_2026_STATEMENT.pdf"
            )
            (downloads / property_name).write_bytes(b"property")
            (downloads / generic_name).write_bytes(b"generic")
            (downloads / personal_name).write_bytes(b"personal")

            property_moves, unmapped = operator.planned_property_downloads(
                downloads, root / "real-estate", "JUL", 2026
            )
            personal, holdings = operator.planned_non_property_downloads(
                downloads,
                root / "real-estate",
                "JUL",
                2026,
                root / "personal",
                root / "holdings",
            )

            self.assertEqual([item["name"] for item in property_moves], [property_name])
            self.assertIn("NY/88 Madison Ave Public", property_moves[0]["target"])
            self.assertEqual(unmapped, [])
            self.assertEqual([item["name"] for item in personal], [personal_name])
            self.assertIn("personal/2026", personal[0]["target"])
            self.assertEqual([item["name"] for item in holdings], [generic_name])
            self.assertIn(
                "holdings/ECO Systems LLC/Financials/Bank Statements/2026",
                holdings[0]["target"],
            )

    def test_eco_property_account_names_have_dropbox_destination_rules(self):
        cases = {
            "10724 GOODING AVE OPERATIONS": "OH/10724 Gooding Ave",
            "16713 LOTUS DR OPERATIONS": "OH/16713 Lotus Dr",
            "3493 WEST 119TH ST OPERATIONS": "OH/3493 W 119th St",
            "3905 EAST 189TH ST": "OH/3905 E 189th St",
            "402 N WILD OLIVE OPERATIONS": "FL/402 N Wild Olive Ave",
            "4318 CLYBOURNE AVE OPERATIONS": "OH/4318 Clybourne Ave",
            "8708 WILLARD AVE OPERATIONS": "OH/8708 Willard Ave",
            "917 PAWNEE AVE RESERVES": "TN/917 Pawnee Ave",
        }
        root = Path("/tmp/real-estate")

        for account_name, expected in cases.items():
            with self.subTest(account_name=account_name):
                filename = (
                    f"BASELANE_ECO SYSTEMS, LLC_{account_name}_"
                    "JUL_2026_STATEMENT.pdf"
                )
                destination = operator.resolve_dest(root, filename, 2026)
                self.assertIsNotNone(destination)
                self.assertIn(expected, str(destination))


if __name__ == "__main__":
    unittest.main()
