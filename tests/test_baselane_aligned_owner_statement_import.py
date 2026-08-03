from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from scripts.baselane_aligned_owner_statement_import import (
    PlannedRow,
    existing_ledger_fingerprints,
    live_transaction_fingerprint,
    manifest_row_fingerprint,
    planned_ledger_fingerprint,
    planned_live_fingerprint,
    planned_manifest_fingerprint,
    planned_row_to_ledger_row,
)


class BaselaneAlignedOwnerStatementImportTests(unittest.TestCase):
    def test_ledger_row_uses_only_destination_fields(self):
        row = PlannedRow(
            property_short="1456 W 85th St.",
            property_id="property-id",
            row_number=1,
            source_date="2026-07-30",
            date="2026-07-30",
            merchant_name="Aligned",
            description="Owner statement row",
            amount=Decimal("-10.00"),
            source_type="Expense",
            source_category="Property Management",
            source_subcategory="Property Management Fees",
            note="idempotency-key",
            idempotency_key="idempotency-key",
            tag_id="tag-id",
            rich_category="Property Management",
            rich_tag_reason="source category",
        )
        fieldnames = [
            "Date",
            "Merchant",
            "Description",
            "Amount",
            "Type",
            "Category",
            "Sub-category",
            "Property",
            "Unit",
            "Notes",
        ]

        result = planned_row_to_ledger_row(row, fieldnames)

        self.assertEqual(list(result), fieldnames)
        self.assertNotIn("Account", result)
        self.assertEqual(result["Amount"], "-10.00")

    def test_semantic_fingerprint_matches_existing_aligned_row(self):
        row = PlannedRow(
            property_short="1456 W 85th St",
            property_id="property-id",
            row_number=1,
            source_date="July 01, 2026",
            date="2026-07-01",
            merchant_name="Aligned Properties",
            description="Management Fees for 06/2026",
            amount=Decimal("-124.13"),
            source_type="Operating Expenses",
            source_category="Management Fees",
            source_subcategory="",
            note="key=aligned-new-key",
            idempotency_key="aligned-new-key",
            tag_id="80",
            rich_category="Property Management",
            rich_tag_reason="management fee richer tag",
        )
        contents = (
            "Date,Merchant,Description,Amount,Type,Category,Sub-category,Property,Unit,Notes\n"
            '"July 01, 2026",Aligned Properties,Aligned Properties,-124.13,Operating Expenses,'
            'Property Management,,1456 W 85th St.,,"key=aligned-existing-key"\n'
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ledger.csv"
            path.write_text(contents, encoding="utf-8")

            fingerprints = existing_ledger_fingerprints(path)

        self.assertIn(planned_ledger_fingerprint(row), fingerprints)

    def test_manifest_fingerprint_ignores_overlapping_source_key(self):
        row = PlannedRow(
            property_short="1456 W 85th St",
            property_id="81428",
            row_number=1,
            source_date="July 01, 2026",
            date="2026-07-01",
            merchant_name="Aligned Properties",
            description="Management Fees for 06/2026",
            amount=Decimal("-124.13"),
            source_type="Operating Expenses",
            source_category="Management Fees",
            source_subcategory="",
            note="key=aligned-new-key",
            idempotency_key="aligned-new-key",
            tag_id="80",
            rich_category="Property Management",
            rich_tag_reason="management fee richer tag",
        )
        manifest_row = {
            "propertyId": "81428",
            "date": "2026-07-01",
            "amount": -124.13,
            "merchantName": "Aligned Properties",
            "idempotency_key": "aligned-existing-key",
        }

        self.assertEqual(
            planned_manifest_fingerprint(row),
            manifest_row_fingerprint(manifest_row),
        )

    def test_semantic_fingerprints_ignore_wrapped_statement_role_suffix(self):
        row = PlannedRow(
            property_short="1456 W 85th St",
            property_id="81428",
            row_number=1,
            source_date="July 07, 2026",
            date="2026-07-07",
            merchant_name="Angela Heath Receipt",
            description="Rent Income - July 2026",
            amount=Decimal("500.00"),
            source_type="Revenue",
            source_category="Long Term Rents",
            source_subcategory="",
            note="key=aligned-new-key",
            idempotency_key="aligned-new-key",
            tag_id="136",
            rich_category="Long Term Rents",
            rich_tag_reason="rent",
        )
        live_row = {
            "propertyId": "81428",
            "date": "2026-07-07",
            "amount": 500,
            "merchantName": "Angela Heath",
            "tagId": "136",
        }
        manifest_row = {
            "propertyId": "81428",
            "date": "2026-07-07",
            "amount": 500,
            "merchantName": "Angela Heath",
        }

        self.assertEqual(planned_live_fingerprint(row), live_transaction_fingerprint(live_row))
        self.assertEqual(planned_manifest_fingerprint(row), manifest_row_fingerprint(manifest_row))


if __name__ == "__main__":
    unittest.main()
